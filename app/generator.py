"""报告生成：事实台账 + 材料文本 → 内评简评报告草稿。

分工原则（关键）：
- 表格里的数字由程序从财汇事实台账直接生成，不让模型重算，避免数字失真；
- 模型的职责只有「写判断句和段落」，并在无法确定时显式标注；
- 生成后做数字溯源校验，把正文里对不上台账与原文的数字标出来。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import llm
import usage as usage_mod

BASE = Path(__file__).resolve().parent.parent
TEMPLATE_JSON = BASE / "01_模板" / "内评简评报告通用模板_v1.1.json"
METHOD_MD = BASE / "02_方法" / "内评简评报告写作方法论_v1.1.md"

# 财务表行项的排列位置：规模 → 结构 → 盈利 → 获现 → 债务 → 偿债。
# 行项本身由模板 indicator_layers 的触发条件决定，这里只负责排序，
# 不承担「哪个行业看哪几行」的判断。
ROW_ORDER = [
    "资产总额", "负债总额", "所有者权益", "资产负债率",
    "存货", "应收账款", "在建工程", "固定资产",
    "营业总收入", "营业成本", "营业毛利率",
    "净利润", "利润总额", "投资收益", "公允价值变动损益",
    "EBITDA", "经营活动现金流量净额",
    "短期债务", "长期债务", "全部债务", "总资本化比率",
    "货币资金/短期债务", "EBITDA利息保障倍数",
]


def _num(d: dict, field: str) -> float | None:
    v = d.get(field)
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _equity(d: dict) -> float | None:
    e = _num(d, "所有者权益")
    if e is not None:
        return e
    a, l = _num(d, "总资产"), _num(d, "总负债")
    return round(a - l, 2) if a is not None and l is not None else None


def _calc(name: str, d: dict, equity: float | None) -> float | None:
    """自算比率。台账没有现成字段的行在这里算，口径写在模板里。"""
    if name == "total_capitalization":
        debt = _num(d, "有息债务")
        return round(debt / (debt + equity) * 100, 2) if debt and equity else None
    if name == "cash_to_short_debt":
        cash, sd = _num(d, "货币资金"), _num(d, "短期债务")
        return round(cash / sd, 2) if cash is not None and sd else None
    if name == "debt_ratio":
        l, a = _num(d, "总负债"), _num(d, "总资产")
        return round(l / a * 100, 2) if l is not None and a else None
    if name == "gross_margin":
        rev, cost = _num(d, "营业总收入"), _num(d, "营业成本")
        return round((rev - cost) / rev * 100, 2) if rev and cost is not None else None
    return None


def _trigger_ok(when: dict, d: dict) -> bool:
    """解释模板里的 when 规则，决定某行是否出现。"""
    if not isinstance(when, dict) or not when:
        return True
    if "all_of" in when:
        return all(_trigger_ok(w, d) for w in when["all_of"])
    if "any_of" in when:
        return any(_trigger_ok(w, d) for w in when["any_of"])
    t = when.get("type")
    if t == "field_available":
        return _num(d, when.get("field", "")) is not None
    if t == "gt_zero":
        v = _num(d, when.get("field", ""))
        return v is not None and v > 0
    if t in ("ratio_gte", "abs_ratio_gte"):
        n, den = _num(d, when.get("num", "")), _num(d, when.get("den", ""))
        if n is None or not den:
            return False
        r = n / den if t == "ratio_gte" else abs(n / den)
        return r >= float(when.get("threshold", 0))
    return False


def _probe_period(periods: list[str], data: dict) -> dict:
    """触发判断用的样本期：优先最近完整年度，没有则用最新一期。"""
    for p in periods:
        if p.endswith("年报") and data.get(p):
            return data[p]
    for p in periods:
        if data.get(p):
            return data[p]
    return {}


def _fmt(v: float | None, fmt: str) -> str:
    if v is None:
        return "--"
    if fmt == "pct":
        return f"{v:.2f}%"
    if fmt == "x":
        return f"{v:.2f}"
    return f"{v:,.2f}"


def _load_template() -> dict:
    return json.loads(TEMPLATE_JSON.read_text(encoding="utf-8"))


def period_label(p: str) -> str:
    m = re.match(r"(\d{4})年(年报|中报|一季报|三季报|半年报|季报)?", p or "")
    if not m:
        return p
    year, kind = m.group(1), m.group(2) or ""
    return {
        "年报": f"{year}年",
        "中报": f"{year}年6月末",
        "半年报": f"{year}年6月末",
        "一季报": f"{year}年3月末",
        "季报": f"{year}年3月末",
        "三季报": f"{year}年9月末",
    }.get(kind, f"{year}年")


PERIOD_WORDS = {
    "年报": ("{y}年末", "{y}年"),
    "中报": ("{y}年6月末", "{y}年1-6月"),
    "半年报": ("{y}年6月末", "{y}年1-6月"),
    "三季报": ("{y}年9月末", "{y}年1-9月"),
    "一季报": ("{y}年3月末", "{y}年1-3月"),
    "季报": ("{y}年3月末", "{y}年1-3月"),
}


def period_labels(facts: dict) -> dict[str, dict[str, str]]:
    """给模型一份期次写法对照表。

    财汇「报告期」字段用的是「2026年中报」这类口径，模型极易自行改写成
    「2026年1-9月」。这里把时点与区间写法都固定下来，正文照抄即可。
    """
    out: dict[str, dict[str, str]] = {}
    for p in ((facts.get("financials") or {}).get("periods") or []):
        m = re.match(r"(\d{4})年(.+)", p or "")
        if not m:
            out[p] = {"时点": p, "区间": p}
            continue
        y, kind = m.group(1), m.group(2)
        point, span = PERIOD_WORDS.get(kind, ("{y}年末", "{y}年"))
        out[p] = {"时点": point.format(y=y), "区间": span.format(y=y)}
    return out


def describe_traits(facts: dict) -> dict:
    """从台账数据读出主体的结构特征，交给模型判断该写哪些段落。

    刻意不做行业归类：名称与行业标签经常与实际业务不符，一旦归错，后面
    的段落取舍与行项都会跟着错。这里只报可验证的结构事实。
    """
    fin = facts.get("financials") or {}
    probe = _probe_period(fin.get("periods") or [], fin.get("data") or {})
    ta, rev = _num(probe, "总资产"), _num(probe, "营业总收入")
    out: dict[str, Any] = {}
    if ta and rev:
        out["营业收入/总资产"] = round(rev / ta, 3)
    for f, label in (("固定资产", "固定资产占总资产"), ("在建工程合计", "在建工程占总资产"),
                     ("存货", "存货占总资产"), ("应收账款", "应收账款占总资产"),
                     ("货币资金", "货币资金占总资产")):
        v = _num(probe, f)
        if v is not None and ta:
            out[label] = f"{v / ta * 100:.2f}%"
    debt, eq = _num(probe, "有息债务"), _equity(probe)
    if debt and eq:
        out["总资本化比率"] = f"{debt / (debt + eq) * 100:.2f}%"
    sd = _num(probe, "短期债务")
    if debt and sd:
        out["短期债务/有息债务"] = f"{sd / debt * 100:.2f}%"
    if _num(probe, "销售毛利率") is not None:
        out["销售毛利率"] = f"{_num(probe, '销售毛利率'):.2f}%"
    comp = facts.get("composition") or []
    if comp:
        out["披露的主营板块数"] = len(comp)
    out["用法"] = ("以上是结构事实，用来决定写几段业务、资产段重点看哪几个科目；"
                 "不要据此给主体贴行业标签，也不要因为某个比例高就套用固定的行业话术")
    return out


def build_financial_table(facts: dict) -> dict:
    """按模板的三层规则选行：必选 + 条件触发（按占比阈值）+ 主体专属。

    选行只看「字段是否可得、占比是否够大」，不看行业名称。
    台账没有财务数据（只检索公告、或纯本地上传材料）时返回空壳，
    由调用方改用模型从材料原文整理的表。
    """
    tpl = _load_template()
    layers = tpl.get("indicator_layers") or {}
    fin = facts.get("financials") or {}
    periods = list(fin.get("periods") or [])[:3]
    data = fin.get("data") or {}
    empty = {"caption": "公司主要财务数据和财务指标（合并口径）",
             "unit_line": "单位：亿元", "periods": [], "rows": []}
    if not periods or not data:
        return empty
    labels = [period_label(p) for p in periods]
    probe = _probe_period(periods, data)

    spec = list(layers.get("base") or [])
    cond = [r for r in (layers.get("conditional") or [])
            if _trigger_ok(r.get("when") or {}, probe)]
    cond.sort(key=lambda r: r.get("priority", 99))
    limit = int(layers.get("max_rows") or 15)
    spec.extend(cond[:max(0, limit - len(spec))])
    order = {row: i for i, row in enumerate(ROW_ORDER)}
    spec.sort(key=lambda r: order.get(r.get("row"), 99))

    rows = []
    for r in spec:
        rowname = r.get("row") or ""
        field = r.get("field")
        fmt = r.get("fmt") or "yi"
        values = []
        for p in periods:
            d = data.get(p) or {}
            equity = _equity(d)
            v = _num(d, field) if field else None
            if v is None and r.get("fallback") == "总资产-总负债":
                v = equity
            if v is None and r.get("fallback_calc"):
                v = _calc(r["fallback_calc"], d, equity)
            if v is None and r.get("calc"):
                v = _calc(r["calc"], d, equity)
            values.append(_fmt(v, fmt))
        label = "货币资金/短期债务（倍）" if rowname == "货币资金/短期债务" else rowname
        rows.append([label] + values)

    return {
        "caption": "公司主要财务数据和财务指标（合并口径）",
        "unit_line": "单位：亿元",
        "periods": labels,
        "rows": rows,
    }


def build_segment_table(facts: dict) -> dict | None:
    comp = facts.get("composition") or []
    if not comp:
        return None
    fields = set(comp[0].keys())
    name_key = next((k for k in ("主营构成", "产品", "业务板块", "项目", "行业") if k in fields), None)
    rev_key = next((k for k in ("主营收入", "营业收入", "收入") if k in fields), None)
    cost_key = next((k for k in ("主营成本", "营业成本", "成本") if k in fields), None)
    ratio_key = next((k for k in ("收入占比", "占比") if k in fields), None)
    profit_key = next((k for k in ("毛利", "利润") if k in fields), None)
    if not name_key or not rev_key:
        return None

    rows, total = [], 0.0
    for c in comp[:15]:
        name = str(c.get(name_key) or "").strip()
        if not name:
            continue
        try:
            rev = float(c.get(rev_key) or 0) / 10000.0
        except (TypeError, ValueError):
            continue
        total += rev
        gm = ""
        if ratio_key and c.get(ratio_key) not in (None, ""):
            try:
                gm = f"{float(c[ratio_key]):.2f}%"
            except (TypeError, ValueError):
                gm = ""
        rows.append([name, f"{rev:,.2f}", gm])
    if not rows:
        return None
    # 统一按收入占比列填充：若无占比列，用收入合计自算
    if not ratio_key:
        for r in rows:
            try:
                r[2] = f"{float(r[1].replace(',', '')) / total * 100:.2f}%" if total else ""
            except ValueError:
                r[2] = ""
    table_rows = [[r[0], r[1], r[2], ""] for r in rows]
    table_rows.append(["营业总收入", f"{total:,.2f}", "100.00%", ""])
    return {
        "unit_line": "单位：亿元",
        "header": ["业务板块", "营业收入", "收入占比", "毛利率"],
        "rows": table_rows,
    }


SYSTEM_PROMPT = """你是信用评级机构的分析员，负责撰写内部「简评报告」。写作必须严格遵循给定的体例与纪律。

【体例】报告只有两层：摘要层（正面：/关注：各3条，最多5条，编号 1）2）3））与正文层（1、基本情况；2、点评）。
点评由以下块按序组成：总述段 → 业务构成表 → 分部业务段（1）2）…）→ 资产段 → 财务指标表 → 债务段 → 或有/专题段（可选）。

【硬规则】
1. 摘要层是定性概括，除判断核心依赖量级外不得出现金额、百分比、倍数、占比。判定口径：删掉这个数字，这条判断是否仍成立？成立就删掉数字。
2. 摘要一条只讲一件事，正面与关注不得重复同一事实，不得互相矛盾。
3. 正文每段以一句加粗汇总句开篇（总述段、资产段、债务段、专题段），汇总句带判断不带数字；分部业务段不加段首加粗。
4. 金额统一亿元、两位小数、带千分位；比率两位小数加百分号；倍数两位小数。期次写明「2025年」「2026年9月末」「2026年1-9月」。原始表格多以万元计（也有以元、美元计的），先换算再写，不得把万元数直接当亿元；同段落里的量级要能相互印证——按总资产规模判断，货币资金、有息债务不可能是差着两三个数量级的数。
5. 不得虚构任何数字、项目、名称。材料里没有的信息就不写，不得推算、不得用行业均值替代；也不要在正文里交代「哪一项没拿到」——规则 9 的某一拍缺数据就整拍跳过，不要写成「××方面，材料未披露」。财务指标表单元格确实无值的填「—」。
6. 不得照抄评级机构的结论句作为自己的判断；可复算的要用自己的口径重述。不得出现「中诚信国际认为」「评级时应注意到」这类元话语。
7. 业务分部段六拍：业务模式与产出能力 → 报告期的产出量 → 价格或费率与收入 → 成本结构与毛利率变动原因 → 项目或订单储备 → 该板块风险点。产出量的计量单位按业务性质选（取该公司连续披露、可跨期比较、且与收入同向的那一个量），不同板块不必统一；找不到就跳过这一拍，不拿无关指标充数。单个板块的正文至少三句：做的是什么、当期经营数据、风险点，各占一句以上；收入占比不足 5% 的板块并成一段「其他业务」写，不要为凑板块数量把一句话拆成一段。
8. 资产段的前五大科目必须按最新期次的实际金额降序排列（不是沿用旧名单换数字），写清每个科目「是什么」，并交代其质量——能否变现、是否已质押、账龄或回收对象。
9. 债务段按固定顺序展开：负债总额与资产负债率 → 全部债务及长短债结构与占比 → 有息债务的融资构成 → 利息保障与经营获现 → 非受限货币资金对短债覆盖 → 授信与未使用额度。或有事项不进债务段——它由用户在页面上自行决定是否单列一段（见规则 21），债务段里不写「或有事项方面，……」。
10. 全文用合并口径。总债务（含政府专项债等）与有息债务是两个口径，混用时必须说明差额来源。
11. 期次只能照抄台账『期次写法』里的字面写法：时点用「X年末／X年6月末／X年9月末」，区间用「X年／X年1-6月／X年1-9月」。资产类、负债类、债务类数据用时点写法，收入、利润、现金流用区间写法。台账写「中报」的，时点就是「X年6月末」，绝不允许写成 9 月末或 1-9 月。
12. 不按行业套路写作。写外部条件时问三件事：钱从谁那里来、量由什么决定、价由谁定；三问的答案决定引什么数据。不要因为主体名称或行业标签就推断它应该有什么业务、什么风险——只写材料里有据、且该主体确实存在的事实。找不到某项惯常指标（例如某类产出量），就跳过，不用相近指标替代。
13. 叙述视角：报告是评级机构对受评主体的判断，句中主语只能是公司、股东、政府、行业或政策，不能是「材料」。全文禁止出现「材料」「材料原文」「公开资料」「公开信息」「本报告」「本文」，禁止出现「披露」「未披露」「载明」「列示」「数据显示」「报表显示」这类转述来源的词，也禁止「从外部条件看」「从内部条件看」「从财务表现看」这类框架提示语——直接陈述事实与因果。判断标准：把这些词删掉后句子依然成立，就说明本来不该写。
14. 正文不出现编制层面的说明：不写「因数据可得性」「受限于资料」「以××为准」「详见下表」「如下所示」。表格由程序排版，正文只管判断。
15. 概念不得混用：已实现营业收入不等于合同金额、中标金额或订单储备；净利润不等于 EBITDA，也不等于经营活动现金流；年度数据不等于单季或累计数据；历史数据不等于预测数。用之前先确认是哪一种，口径不同时在句中点明。
16. 变化要算幅度并归因：收入、利润、毛利率、资产负债率、有息债务等关键指标，与上期相比变动超过 5% 的，写出变动幅度并说明成因（量、价、结构、并表范围、政府补助、会计口径调整），不要只并列两期数字让读者自己算。
17. 措辞分级、不用极端词与宣传语：判断词只用很强／较强／一般／较弱、可控／充足／尚可／承压这类分级表述，不用「极强」「极弱」「恶劣」「极大」「严重恶化」；材料里的「荣获」「行业领先」「填补空白」「有力保障」「成效显著」等宣传语与美化表述一律不进正文，只写可核实的事实与风险。
18. 正文每段至少锚定一个具体数据或明确事实（金额、比率、期限、数量、协议条款），不写通篇定性的空段；也不为凑数据引与判断无关的数字——引的每个数都要能用在一句判断上。
19. 同一件事只说一遍。加粗汇总句是判断，正文不得把这句话换个说法再说一次；专题段只写它自己那件事的新证据，不复述债务段、资产段或业务段已经写过的数据与结论；同一条事实（如某科目余额、某协议条款）在全文只出现一次，后文需要引用时用「上述」带过或直接给结论，不重抄原句。
20. 分部业务段是一整段：编号与板块名之后的全部内容在同一段里连续写完，不在段内换行、不分成若干小段、不加小标题。
21. 摘要从正文倒抽，不从材料正推。写完正文后再回头逐段读，把「能独立成立、且对偿债有含义」的判断句圈出来当候选池：先圈 10-14 条进 positive_pool / concern_pool（每条 25-45 字、纯定性、不带数字、不写「公司」开头以外的修饰），再从中按分量排序挑 3-5 条进 positive / concern。定稿的条目必须同时出现在候选池里。候选池里不要放正文没说过的话——它只是正文的抽提，不是第二遍分析。
22. 专题段（special_topics）默认输出空数组。只有材料里存在「非报不可」的重大事项（大额对外担保且被担保方已出现风险、重大未决诉讼、股东出资未到位、在建项目投资缺口远超货币资金）才写，且最多一条。写的时候必须带一句独立的加粗判断句作 lead（先说结论，不带数字），body 再摆数据；不要用「或有事项方面，」这种引子开头。用户在页面上可自行增删专题段，你不必为了完整而补。
19. 两类专项按条件触发，不按行业标签触发。（1）收入主要来自政府结算（土地整理、代建、特许经营、购买服务）的，总述段交代「钱从谁来」时就要带上区域财力的实数——地区 GDP 与增速、一般公共预算收入、政府性债务余额或债务率，各一句、写明年份，并点明公司在这套结算体系里的层级与回款路径；不要只写「区域财力持续增长」这种不带数的判断，也不要把区域情况另起一段展开。（2）在建工程占非流动资产比重较高的，写清计划总投资、已累计投资、剩余投资，并与货币资金对照说明缺口。材料没有这些数据就整段不写，不许用「预计」「大致」补位。

【写作风格】以跟踪评级报告正文为语体基准：段首归因（受……影响／受益于）、段中展开（具体来看／结构上／此外）、段末收口（整体来看，……）。判断词要分级（很强/较强/一般/较弱、可控/充足/尚可/承压）。不用「本期」「去年」「大幅」这类不可核实表述，也不用「可能」「大概」「或许」——判断要么有据，要么不写。行文里不出现「材料」「披露」「外部条件」这类词：它们是分析员的作业对象，不是报告的语言。

【输出】只输出一个 JSON 对象，不要任何解释文字。"""


def build_user_prompt(facts: dict, materials: list[dict]) -> str:
    tpl = _load_template()
    tmpl_brief = {
        "sections": [
            {"id": s["id"], "title": s.get("title"), "type": s["type"],
             "word_range": s.get("word_range"), "beats": s.get("beats")}
            for s in tpl["sections"]
        ],
        "mark_source_types": {
            "positive": [t["type"] for t in tpl["sections"][1].get("source_types") or []],
            "concern": [t["type"] for t in tpl["sections"][2].get("source_types") or []],
        },
        "external_condition_questions": tpl["design_principle"]["external_condition_questions"],
    }
    has_fin = bool((facts.get("financials") or {}).get("periods"))
    fact_view: dict[str, Any] = {
        "企业": facts.get("company", {}).get("name"),
        "结构特征（由台账算出，用来决定写什么；不要据此贴行业标签）": describe_traits(facts),
    }
    for label, val in (
        ("工商指标", facts.get("company", {}).get("indicators")),
        ("实际控制人", (facts.get("equity") or {}).get("controller")),
        ("直接股东", (facts.get("equity") or {}).get("shareholders")),
        ("主体评级历史", (facts.get("ratings") or [])[:5]),
        ("存量债券", (facts.get("bonds") or [])[:8]),
        ("财务指标（亿元/%，程序已换算）", (facts.get("financials") or {}).get("data")),
        ("期次写法（正文严格照此书写，不得改写）", period_labels(facts)),
        ("主营构成（原始，单位万元）", (facts.get("composition") or [])[:12]),
    ):
        if val:
            fact_view[label] = val
    if has_fin:
        fact_view["口径提示"] = [
            "台账中的『短期债务/长期债务/有息债务』为有息债务口径，可能与评级报告的『全部债务』不同，引用时须注明口径。",
            "财汇结构化数据与评级报告原文不一致时，正文以【材料原文】为准，并在 data_notes 里记下差异。",
        ]
    else:
        fact_view["口径提示"] = [
            "本次未取结构化台账：工商信息、股权、财务数据、主营构成一律以【材料原文】为准，材料没写的信息直接略过，不得推算；正文里不要交代哪项没拿到。",
            "期次写法照材料原文的表述，同一张表内的期次必须与材料表的表头一致。",
            "财务指标表需要你按【材料原文】的「主要财务数据」表整理输出，数字与材料逐一对应。",
        ]
    mat_block = "\n\n".join(f"===== {m['title']}（{m.get('date','')}）=====\n{m['text']}"
                            for m in materials if m.get("text"))
    schema = """{
  "company_full_name": "工商全称",
  "positive": ["定稿的正面观点，3-5 条", "..."],
  "concern": ["定稿的关注观点，3-5 条", "..."],
  "positive_pool": ["候选：从正文回抽、够格但没进定稿的正面判断，5-8 条"],
  "concern_pool": ["候选：从正文回抽、够格但没进定稿的关注判断，5-8 条"],
  "basic_info": "150-300字，一段：成立与前身→主营与功能定位（含靠什么收钱）→注册资本/实收资本/股东及比例/实控人→报告期股权变动→增资、上市、外部评级、并表子公司",
  "commentary": {
    "overview": {"lead": "加粗汇总句（不带数字）", "body": "总述段正文：先交代钱的来源、量的决定、价的制定，再写经营与盈利主线数据与收入构成概览，收口引出业务构成表"},
    "segments": [{"name": "该主体自己用的板块名，照原文写，不改写不带编号", "body": "承接『1）××业务方面，』之后的正文"}],
    "segment_table": {"unit_line": "单位：亿元", "header": ["业务板块", "营业收入", "收入占比", "毛利率"], "rows": [["板块名", "0.00", "0.00%", ""], ["营业总收入", "0.00", "100.00%", ""]]},
    "financial_table": {"unit_line": "单位：亿元", "periods": ["2023年", "2024年", "2025年"], "rows": [["资产总额", "0.00", "0.00", "0.00"], ["负债总额", "0.00", "0.00", "0.00"]]},
    "asset": {"lead": "加粗汇总句（不带数字）", "body": "资产段正文"},
    "debt": {"lead": "加粗汇总句（不带数字）", "body": "债务段正文"},
    "special_topics": []
  },
  "data_notes": ["口径差异或不确定处，逐条说明"],
  "sources": [{"claim": "文中的关键判断或数字", "where": "材料名+页码/章节"}]
}"""
    return f"""请为下列主体撰写内评简评报告，按给定 JSON 结构输出。

【模板结构】{json.dumps(tmpl_brief, ensure_ascii=False)}

【事实台账】
{json.dumps(fact_view, ensure_ascii=False, indent=1)}

【材料原文】
{mat_block}

【输出 JSON 结构】
{schema}

注意：若【事实台账】已有「财务指标」，财务指标表由程序按台账生成，你不需要输出 financial_table；若台账没有财务指标，你必须从【材料原文】的「主要财务数据」表整理 financial_table——期次取最近三期（前两年 + 最新一期，按时间正序排列，最新一期放最后），期次写法照抄材料表头，行项按规模→结构→盈利→获现→债务→偿债排列，金额统一亿元、两位小数，数字与材料逐一对应，材料没有的行不要编。若台账无主营构成数据，请从评级报告正文补出 segment_table（金额按亿元、两位小数，占比两位小数，缺毛利率则该列留空字符串），否则 segment_table 输出 null。你只需保证正文里的数字与材料一致，且能被台账或材料原文支撑。segments 的 name 用该公司自己披露的板块名称，不要带「业务方面」四个字，也不带编号；板块数量按收入结构决定，不要为了凑段数拆分或合并材料里没有的板块。special_topics 默认给空数组（用户在前端按需加段）；确有重大事项才写一条，且必须自带加粗判断句作 lead。"""


def _clean_llm_table(t: Any) -> dict | None:
    """校验并清洗模型给出的业务构成表。"""
    if not isinstance(t, dict):
        return None
    rows = t.get("rows") or []
    if not isinstance(rows, list) or len(rows) < 2:
        return None
    clean_rows = []
    for r in rows:
        if not isinstance(r, (list, tuple)) or not r:
            continue
        clean_rows.append([str(c).strip() if c is not None else "" for c in list(r)[:4]]
                          + [""] * max(0, 4 - len(r)))
    if len(clean_rows) < 2:
        return None
    return {
        "unit_line": t.get("unit_line") or "单位：亿元",
        "header": t.get("header") or ["业务板块", "营业收入", "收入占比", "毛利率"],
        "rows": clean_rows,
    }


_LEAD_NUM_RE = re.compile(
    r"^\s*(?:[（(]\s*[0-9一二三四五六七八九十]{1,2}\s*[）)]"
    r"|[0-9]{1,2}\s*[）)、.．]"
    r"|[一二三四五六七八九十]{1,2}\s*[、）)])\s*")


def clean_segment_name(name: str) -> str:
    """板块名只留主体部分：去掉编号与『业务/板块/方面』这类后缀。"""
    n = _LEAD_NUM_RE.sub("", str(name or "")).strip()
    n = re.sub(r"(?:业务方面|方面|板块)$", "", n).strip()
    return n


def clean_segment_body(name: str, body: str) -> str:
    """去掉模型自带的导语。

    程序排版时会统一加「1）××业务方面，」，模型若已写了同样的开头，
    docx 里就会出现「2）新材料业务方面，2）新材料业务方面，」这种重复句。
    """
    b = _LEAD_NUM_RE.sub("", str(body or "").strip())
    for _ in range(2):  # 模型偶尔把导语写两遍
        before = b
        if name:
            b = re.sub(rf"^{re.escape(name)}\s*(?:业务)?方面\s*[，,、：:]?\s*", "", b)
        # 板块名与模型写法略有出入时（如「新材料」与「新材料业务」）再兜一层
        b = re.sub(r"^[^，。；：\n]{2,16}?业务方面\s*[，,、：:]\s*", "", b, count=1)
        if b == before:
            break
    return b.strip()


# ── 叙述视角：报告的主语是公司，不是「材料」 ────────────────────────────
# 模型写材料摘要写惯了，正文里会漏出「材料未披露」「从外部条件看」这类
# 交代来源与框架的串场话。评级报告的判断句里没有这些词，删掉后句子照样成立。
_VIEW_WORDS = re.compile(
    r"材料|公开资料|公开信息|外部资料|本报告|本文|披露|载明|列示|详见|"
    r"资料来源|可得资料|未获取|数据表显示|数据显示|报表显示|"
    r"未取数|未采集|数据缺失|缺数据|缺失数据|口径缺失|未纳入统计")
# 「原材料」「新材料」是业务名，不是「材料未披露」的那个材料，先遮住再判
_SAFE_WORDS = ("原材料", "材料采购", "材料成本", "材料供应", "材料费用", "材料费",
               "建筑材料", "新材料", "包装材料", "材料价格")
# 「授信方面，」「负债结构上，」这类只剩话题引子的碎句，删掉主要内容后一并清掉
_HANG_WORDS = re.compile(r"^[^，。；：]{2,18}(?:方面|方面上|层面|上|来看)[，；]$")
# 「从外部条件看，」这类框架提示语：删掉后直接进入事实与因果
_FRAME_LEAD = re.compile(
    r"从(?:外部|内部|自身|经营|财务|区域|行业|政策|产品|业务|数据|报表|资料|"
    r"公开信息|公开资料|材料)[^，。；]{0,6}?(?:看|来看|观察|而言)[，,]\s*")

# 先擦掉「材料显示」「据材料」这类前缀，多数句子擦完就能用，不必整块删
_ERASE_RULES = (
    re.compile(r"[^，。；\s]{0,10}?(?:数据显示|报表显示)[，,]?\s*"),
    re.compile(r"(?:根据|据)\s*(?:材料|公开资料|公开信息)(?:原文|内容|记载)?[，,]?\s*"),
    re.compile(r"(?:材料|公开资料|公开信息)(?:原文)?"
               r"(?:显示|表明|载明|列示|披露|记载)(?:了|的)?[，,]?\s*"),
    re.compile(r"(?:材料|公开资料|公开信息)(?:原文)?(?:中|里|上)[，,]?\s*"),
    re.compile(r"(^|[，；])(?:另)?(?:据)?(?:披露|载明|列示)(?:的|，)"),
    re.compile(r"详见(?:下|上|后)?(?:表|文|图)[，,]?\s*"),
)


# 这些是真的拿不到东西，句子留着只会显得在交代作业过程，应整句删（由 _VIEW_WORDS 命中）
# 「同比大幅下降」这类程度词不可核实，同段通常已写明百分比，直接去掉程度词
_VAGUE_DEGREE = re.compile(
    r"(?:大幅|大幅度|显著)(?=(?:下降|上升|增长|减少|增加|提高|回落|波动|改善|恶化|提升))")


def _mask_terms(text: str) -> tuple[str, dict[str, str]]:
    """把「原材料」这类安全词换成占位符，避免被当成来源交代删掉。"""
    book: dict[str, str] = {}
    for i, w in enumerate(_SAFE_WORDS):
        if w in text:
            tok = f"\x01{i}\x01"
            text = text.replace(w, tok)
            book[tok] = w
    return text, book


def _unmask_terms(text: str, book: dict[str, str]) -> str:
    for tok, w in book.items():
        text = text.replace(tok, w)
    return text


def has_view_words(text: str) -> bool:
    """给校验用：这段是不是还在交代资料来源。"""
    masked, book = _mask_terms(text or "")
    return bool(_VIEW_WORDS.search(masked))


def polish_language(text: str) -> str:
    """删掉正文里的来源交代与框架提示语，只留公司层面的判断。

    两步：绝大多数情况只需擦掉「材料显示」这类前缀，句子本身就成立；
    擦不干净的（「××方面，材料未披露。」）再按小句摘除。
    """
    t = (text or "").strip()
    if not t:
        return t
    t = _FRAME_LEAD.sub("", t)
    t = _VAGUE_DEGREE.sub("", t)
    masked, book = _mask_terms(t)
    for pat in _ERASE_RULES:
        if pat.groups:
            masked = pat.sub(lambda m: m.group(1) or "", masked)
        else:
            masked = pat.sub("", masked)
    if not _VIEW_WORDS.search(masked):
        return _unmask_terms(_tidy_punct(masked), book)

    kept_sents: list[str] = []
    for sent in re.split(r"(?<=[。！？])", masked):
        if not sent.strip():
            continue
        if _DANGLING.match(sent.strip()):
            continue  # 「…形成约束。和公司土地整理…」这类粘连句，删掉
        if not _VIEW_WORDS.search(sent):
            kept_sents.append(sent)
            continue
        # 句内还带来源交代：按逗号、分号拆小句，只摘掉带来源词的那几块
        pieces = re.split(r"(?<=[，；])", sent)
        kept = [p for p in pieces if not _VIEW_WORDS.search(p)]
        kept = [p for p in kept if not _HANG_WORDS.match(p)]
        if not kept:
            continue  # 整句都是在交代来源，直接不要
        body = "".join(kept).rstrip("，；、 ")
        tail = sent[-1] if sent[-1] in "。！？" else ""
        kept_sents.append(body + tail)
    return _unmask_terms(_tidy_punct("".join(kept_sents)), book)




# 以「和/与/及」开头的句子，多半是模型把上一段末尾与下一段开头粘在一起留下的病句
_DANGLING = re.compile(r"^\s*(?:和|与|及|以及|并且)(?=公司|该|上述|其|主体)")


def _tidy_punct(s: str) -> str:
    s = re.sub(r"[，；、]{2,}", "，", s or "")
    s = re.sub(r"([。！？])\1+", r"\1", s)      # 删句后常留下「。。」
    s = re.sub(r"^[，；、。\s]+", "", s)
    s = re.sub(r"[，、]\s*(?=[。！？])", "", s)
    s = re.sub(r"[，；、]+$", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


# ── 去重：同一件事只说一遍 ────────────────────────────────────────────────
# 模型爱把加粗汇总句的判断在正文里再说一遍，读起来就是「这句怎么又来了」。
# 判据是字面重复，不做语义判断：连续 14 个汉字相同才动手，改写的重复交给校验提示。
_DEDUP_LOG: list[str] = []


def _core_text(s: str) -> str:
    return re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9%]", "", s or "")


def _common_run(a: str, b: str) -> int:
    """最长公共连续片段长度。"""
    if not a or not b:
        return 0
    from difflib import SequenceMatcher
    m = SequenceMatcher(None, a, b, autojunk=False).find_longest_match(0, len(a), 0, len(b))
    return m.size


def _common_text(a: str, b: str) -> str:
    if not a or not b:
        return ""
    from difflib import SequenceMatcher
    m = SequenceMatcher(None, a, b, autojunk=False).find_longest_match(0, len(a), 0, len(b))
    return a[m.a:m.a + m.size]


_PERIOD_TOK = re.compile(
    r"20\d{2}\s*年(?:\d{1,2}\s*月(?:末|初|底)?|度|末|初|上半年|下半年|第[一二三四]季度)?"
    r"|\d{1,2}\s*月(?:末|初|底)?|\d{1,2}\s*[-—～]\s*\d{1,2}\s*月")

# 各段共用的句式骨架。这些词一起出现是文风一致，不是内容重复，
# 比对前先抹掉，留下的才是「这句话在别处已经说过」的实质部分。
_BOILER = ("该板块收入分别为", "占营业收入的比重分别为", "占营业收入的比重为",
           "占营业收入比重分别为", "较上年同期", "同期末", "截至", "分别为",
           "同比", "主要系", "公司", "亿元", "万元", "个百分点")


def _norm_periods(s: str) -> str:
    """把期次与套话骨架换成占位符再比对。"""
    t = _PERIOD_TOK.sub("@", s or "")
    for w in _BOILER:
        t = t.replace(w, "@")
    return t


def is_real_dup(a: str, b: str, min_len: int = 14, min_han: int = 9) -> bool:
    """两段是否真的重复。

    只看长度会把「2023年、2024年和2025年」「××收入分别为」「公司」这类期次与
    句式骨架判成重复，误删本来不同的句子。所以先归一化，再要求公共片段里
    至少有 min_han 个汉字——剩下的这些字才是「同一件事」。
    """
    block = _common_text(_norm_periods(a), _norm_periods(b))
    if len(block) < min_len:
        return False
    return len(re.sub(r"[^\u4e00-\u9fa5]", "", block)) >= min_han


def dedupe_sentences(text: str, seen: list[str] | None = None, min_len: int = 14) -> str:
    """删掉与 seen（已出现过的句子）字面重复的句子，返回清理后的文本。

    seen 会被就地追加，用于跨段去重：前一段说过的话，后一段不再重复。
    """
    sents = [s for s in re.split(r"(?<=[。！？])", text or "") if s.strip()]
    if len(sents) < 2 and not seen:
        return (text or "").strip()
    seen = seen if seen is not None else []
    kept: list[str] = []
    for s in sents:
        core = _core_text(s)
        dup = len(core) >= 10 and any(is_real_dup(core, prev, min_len) for prev in seen)
        if dup:
            _DEDUP_LOG.append(s.strip()[:40])
            continue
        kept.append(s)
        if len(core) >= 10:
            seen.append(core)
    return "".join(kept).strip()


def dedupe_lead(lead: str, body: str) -> str:
    """正文里若把加粗句又说了一遍，删掉那一句。"""
    core = _core_text(lead)
    if len(core) < 12 or not body:
        return body
    out = []
    for s in re.split(r"(?<=[。！？])", body):
        if s.strip() and is_real_dup(_core_text(s), core, 14):
            _DEDUP_LOG.append(s.strip()[:40])
            continue
        out.append(s)
    return "".join(out).strip()


def segment_heading(i: int, name: str) -> str:
    n = clean_segment_name(name)
    if not n:
        return f"{i}）"
    if n.endswith(("业务", "板块", "产品", "服务", "行业")):
        return f"{i}）{n}方面，"
    return f"{i}）{n}业务方面，"


def _p(progress, pct: int, stage: str, detail: str = ""):
    if progress:
        progress(pct, stage, detail)


_NUM_CELL_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


def _thousands(s: str) -> str:
    """纯数字补千分位：4887.89 → 4,887.89；带 %、倍、-- 的原样返回。"""
    if not _NUM_CELL_RE.match(s or ""):
        return s
    neg = s.startswith("-")
    body = s[1:] if neg else s
    head, _, tail = body.partition(".")
    head = f"{int(head):,}"
    return ("-" if neg else "") + head + ("." + tail if tail else "")


def _table_has_values(t: dict) -> bool:
    for r in (t or {}).get("rows") or []:
        if any(str(c).strip() not in ("", "--") for c in r[1:]):
            return True
    return False


def _clean_fin_table(t: Any) -> dict | None:
    """清洗模型从材料原文整理的财务表（台账无数据时使用）。"""
    if not isinstance(t, dict):
        return None
    periods = [str(p).strip() for p in (t.get("periods") or []) if str(p).strip()]
    if len(periods) < 2:
        return None
    take = min(3, len(periods))  # 最新一期 + 近两年，与参考件一致
    rows = []
    for r in (t.get("rows") or [])[:24]:
        if not isinstance(r, (list, tuple)) or not r:
            continue
        cells = [str(c).strip() if c is not None else "" for c in list(r)]
        if len(cells) < len(periods) + 1 or not cells[0]:
            continue
        cells = [cells[0]] + [_thousands(c) for c in cells[len(cells) - take:]]
        if all(c in ("", "--") for c in cells[1:]):
            continue
        rows.append(cells)
    periods = periods[-take:]
    if len(rows) < 4:
        return None
    return {
        "caption": t.get("caption") or "公司主要财务数据和财务指标（合并口径）",
        "unit_line": t.get("unit_line") or "单位：亿元",
        "periods": periods,
        "rows": rows,
        "from_materials": True,
    }


def _lead(txt: str) -> str:
    """段首加粗句：清理来源交代；万一被清空，宁可留原句，也不让段落没头。"""
    raw = (txt or "").strip()
    out = polish_language(raw)
    return out or raw


def _ensure_structure(draft: dict, facts: dict) -> dict:
    """补齐缺省字段，并把程序生成的表格塞进去。"""
    out: dict[str, Any] = {}
    out["company_full_name"] = draft.get("company_full_name") or facts.get("company", {}).get("name") or facts.get("query_name", "")
    out["internal_rating_code"] = draft.get("internal_rating_code") or ""
    out["traits"] = describe_traits(facts)
    out["positive"] = [s for s in (polish_language(x) for x in (draft.get("positive") or [])) if s][:5]
    out["concern"] = [s for s in (polish_language(x) for x in (draft.get("concern") or [])) if s][:5]
    # 候选池：定稿条目也算候选，去重后一起给前端挑
    for key in ("positive", "concern"):
        picked = out[key]
        seen: list[str] = [x for x in picked]
        pool: list[str] = []
        for raw in (draft.get(key + "_pool") or []):
            t = polish_language(str(raw))
            if len(t) < 12:
                continue
            if any(is_real_dup(t, x, 12, min_han=8) for x in seen):
                continue
            seen.append(t)
            pool.append(t)
        out[key + "_pool"] = pool[:8]
    out["basic_info"] = polish_language((draft.get("basic_info") or "").strip())

    c = draft.get("commentary") or {}
    ov = c.get("overview") or {}
    at = c.get("asset") or {}
    db = c.get("debt") or {}
    fin_table = build_financial_table(facts)
    if not _table_has_values(fin_table):
        fin_table = _clean_fin_table(c.get("financial_table")) or fin_table

    # 按文档顺序过一遍：先清理语言，再删掉段内同一件事的重复句。
    # 只做段内去重——跨段重复多是同一事实在不同语境的必要复述（业务段的风险点、
    # 专题段的核心证据），机器判断不了该留哪一处，交给校验提示由人决定。
    _DEDUP_LOG.clear()
    out["basic_info"] = dedupe_sentences(out["basic_info"])

    def para(p: dict | None, key: str = "body") -> dict:
        p = p or {}
        lead = _lead(p.get("lead"))
        body = dedupe_lead(lead, polish_language(p.get(key) or ""))
        return {"lead": lead, "body": dedupe_sentences(body)}

    overview = para(ov)
    segments = []
    for s in (c.get("segments") or []):
        if not str(s.get("body") or "").strip():
            continue
        nm = clean_segment_name(str(s.get("name") or ""))
        b = polish_language(clean_segment_body(nm, str(s.get("body") or "")))
        segments.append({"name": nm, "body": dedupe_sentences(b)})
    asset = para(at)
    debt = para(db)
    topics = [para(t) for t in (c.get("special_topics") or []) if str(t.get("body") or "").strip()]

    out["commentary"] = {
        "overview": overview,
        "segment_table": build_segment_table(facts) or _clean_llm_table(c.get("segment_table")),
        "segments": segments,
        "asset": asset,
        "financial_table": fin_table,
        "debt": debt,
        "special_topics": [t for t in topics if t["body"]],
    }
    out["data_notes"] = draft.get("data_notes") or []
    out["sources"] = draft.get("sources") or []
    if _DEDUP_LOG:
        out["data_notes"] = list(out["data_notes"]) + [
            f"已自动删去 {len(_DEDUP_LOG)} 处与上文重复的表述：" + "；".join(_DEDUP_LOG[:4])
            + ("……" if len(_DEDUP_LOG) > 4 else "")]
    return out


def polish_draft(draft: dict) -> dict:
    """导出前的最后一道语言清理：只改文字，不动结构与表格。

    页面上用户自己改过的段落也会从这里过一遍——把「材料未披露」「从外部条件看」
    这类话说回来太容易，而导出才是最终形态，不能只保证模型那次干净。
    """
    d = dict(draft or {})
    _DEDUP_LOG.clear()
    changed = 0

    def _pol(s: str) -> str:
        nonlocal changed
        out = polish_language(s)
        if out != (s or "").strip():
            changed += 1
        return out

    d["positive"] = [s for s in (_pol(x) for x in (d.get("positive") or [])) if s]
    d["concern"] = [s for s in (_pol(x) for x in (d.get("concern") or [])) if s]
    for key in ("positive_pool", "concern_pool"):
        if isinstance(d.get(key), list):
            d[key] = [s for s in (_pol(x) for x in d[key]) if s]
    d["basic_info"] = dedupe_sentences(_pol(d.get("basic_info") or ""))

    c = dict(d.get("commentary") or {})

    def para(p):
        p = dict(p or {})
        lead = _lead(p.get("lead"))
        return {"lead": lead, "body": dedupe_sentences(dedupe_lead(lead, _pol(p.get("body") or "")))}

    for k in ("overview", "asset", "debt"):
        if isinstance(c.get(k), dict):
            c[k] = para(c[k])
    if isinstance(c.get("segments"), list):
        c["segments"] = [
            {"name": s.get("name") or "",
             "body": dedupe_sentences(_pol(s.get("body") or ""))}
            for s in c["segments"] if isinstance(s, dict) and (s.get("body") or "").strip()]
    if isinstance(c.get("special_topics"), list):
        c["special_topics"] = [p for p in (para(t) for t in c["special_topics"]
                                          if isinstance(t, dict)) if p["body"]]
    d["commentary"] = c
    n = changed + len(_DEDUP_LOG)
    d["_cleaned"] = n
    if _DEDUP_LOG:
        d["data_notes"] = list(d.get("data_notes") or []) + [
            f"导出前删去 {len(_DEDUP_LOG)} 处重复表述：" + "；".join(_DEDUP_LOG[:3])]
    return d


NUM_RE = re.compile(r"-?\d[\d,]*\.?\d*\s*(?:亿元|万元|%|个百分点|倍)")
# 裸数字（表格里没有单位），用于把材料表格数据收进溯源对照集
BARE_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def collect_numbers(text: str) -> list[str]:
    return [m.group(0).replace(" ", "") for m in NUM_RE.finditer(text or "")]


def _walk_numbers(obj, out: set[str], depth: int = 0):
    """递归收集台账里的所有数值，作为数字溯源的对照集。

    台账里的数值是不带单位的裸数（8.08），正文写的是「8.08亿元」，
    只按带单位文本比对会全部判为不可溯源，因此这里按数值本身入集。
    """
    if depth > 8:
        return
    if isinstance(obj, (int, float)) and not isinstance(obj, bool):
        v = float(obj)
        for txt in (f"{v:.2f}", f"{v:.1f}", f"{v:.0f}", f"{v:.4f}", f"{v:,.2f}"):
            out.add(txt.replace(",", ""))
        # 正文里的负号会在提取数字时被去掉，负值要按绝对值一并入集，
        # 否则「EBITDA 为 -2.11 亿元」会被误判为不可溯源
        out.add(f"{abs(v):.2f}")
        out.add(f"{abs(v):.1f}")
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            _walk_numbers(k, out, depth + 1)
            _walk_numbers(v, out, depth + 1)
    elif isinstance(obj, (list, tuple, set)):
        for v in obj:
            _walk_numbers(v, out, depth + 1)
    elif isinstance(obj, str):
        for m in re.finditer(r"-?\d+(?:\.\d+)?", obj):
            out.add(m.group(0))



# 指标类型猜测：只做「读到数字后面的名词」这一层，不追求准确，
# 目的是让用户知道该去核哪一行，而不是把 14.76% 当成孤立符号。
_KIND_RULES = (
    ("占比", ("占", "比重", "比例", "占比")),
    ("增速", ("增长", "下降", "同比", "环比", "增幅", "降幅")),
    ("结构", ("其中", "结构", "短期", "长期", "流动")),
    ("差额", ("增加", "减少", "变动", "较", "差额", "增量")),
    ("规模", ("余额", "总额", "合计", "规模", "金额")),
    ("比率", ("率", "倍数", "覆盖")),
)


def _guess_kind(sent: str, num: str) -> str:
    """猜这个数字是什么指标。先看单位，再看上下文，别把「95.78亿元」说成占比。"""
    i = (sent or "").find(num)
    if i < 0:
        return ""
    ctx = sent[max(0, i - 14): i + len(num) + 14]
    if "个百分点" in num:
        return "变动幅度"
    if "%" in num:
        if any(w in ctx for w in ("增长", "下降", "同比", "上升", "回落", "增幅", "降幅")):
            return "增速"
        if any(w in ctx for w in ("占", "比重", "比例")):
            return "占比"
        if any(w in ctx for w in ("率", "倍", "覆盖")):
            return "比率"
        return "比例/增速"
    if "倍" in num:
        return "倍数/覆盖率"
    if any(u in num for u in ("亿元", "万元", "元")):
        if any(w in ctx for w in ("增加", "减少", "变动", "较上", "差额", "增量", "多")):
            return "差额"
        if any(w in ctx for w in ("余额", "总额", "合计", "规模")):
            return "规模"
        return "金额"
    return ""


def _cite(sent: str, num: str, width: int = 34) -> str:
    """取数字所在的一小段原文，前后各留点上下文，方便在页面上直接搜到。"""
    s = (sent or "").strip()
    i = s.find(num)
    if i < 0:
        return s[:width * 2]
    left = max(0, i - width)
    right = min(len(s), i + len(num) + width)
    out = s[left:right]
    if left > 0:
        out = "…" + out
    if right < len(s):
        out = out + "…"
    return out


def validate(draft: dict, facts: dict, materials: list[dict]) -> dict:
    """数字溯源校验 + 摘要层数据扫描 + 结构检查。"""
    issues: list[str] = []

    def _sketch(t: str, width: int = 60) -> str:
        """取命中处前后一小段，提示里看得懂是哪句话。"""
        masked, _book = _mask_terms(t or "")
        m = _VIEW_WORDS.search(masked)
        if not m:
            return (t or "")[:width]
        i = m.start()
        return ("…" if i > 12 else "") + t[max(0, i - 12):i + width] + "…"

    # traits 也会进入提示词，模型引用其中的占比属正常，必须纳入可溯源集合
    corpus = (json.dumps(facts, ensure_ascii=False)
              + json.dumps(describe_traits(facts), ensure_ascii=False)
              + "\n" + "\n".join(m.get("text", "") for m in materials))
    corpus_nums = set()
    _walk_numbers(facts, corpus_nums)
    _walk_numbers(describe_traits(facts), corpus_nums)

    def add_nums(s: str):
        for n in collect_numbers(s):
            digits = re.sub(r"[^\d.]", "", n)
            if digits:
                corpus_nums.add(digits)
                try:
                    v = float(digits)
                    corpus_nums.add(f"{v:.2f}")
                    # 材料里的表多按万元计，正文按亿元写；换算后的口径也要认，
                    # 否则「561.57 亿元」会被判成找不到出处。
                    if v >= 10000:
                        corpus_nums.add(f"{v / 10000:.2f}")
                except ValueError:
                    pass

    add_nums(corpus)
    # 材料里的表格数字是裸数（5,615,729.72），不带单位，NUM_RE 抓不到。
    # 单扫一遍：带千分位或小数的照收，并给出万元→亿元的换算值；
    # 纯整数只收大额（页码、年份这类不进集合，免得校验被凑巧命中）。
    for mat in materials:
        for m in BARE_NUM_RE.finditer(mat.get("text") or ""):
            raw = m.group(0)
            clean = raw.replace(",", "")
            try:
                v = float(clean)
            except ValueError:
                continue
            if "," not in raw and "." not in raw and v < 10000:
                continue
            corpus_nums.add(clean)
            corpus_nums.add(f"{v:.2f}")
            if v >= 10000:
                corpus_nums.add(f"{v / 10000:.2f}")
    # 材料里的表格数字常常不带单位（如「19.40」），按裸数值一并入集，
    # 否则正文引用的表格数据会被大面积误判为不可溯源。
    for mat in materials:
        _walk_numbers(mat.get("text") or "", corpus_nums)

    # 1) 摘要层数据扫描
    for key, label in (("positive", "正面"), ("concern", "关注")):
        for i, item in enumerate(draft.get(key) or [], 1):
            nums = collect_numbers(item)
            if nums:
                issues.append(f"{label}{i}）含数据 {nums}，摘要层应为纯定性")

    # 2) 正文数字溯源
    # 带段落标签收集，出问题时要能指出「在哪个段、哪句话、大概是哪个指标」——
    # 只报一串数字（14.76%、0.41亿元）等于让用户自己去全文搜，等于没提。
    labeled: list[tuple[str, str]] = [("基本情况", draft.get("basic_info", ""))]
    c = draft.get("commentary") or {}
    for k, name in (("overview", "总述段"), ("asset", "资产段"), ("debt", "债务段")):
        block = c.get(k) or {}
        labeled.append((name, (block.get("body") or "")))
    for i, seg in enumerate(c.get("segments") or [], 1):
        labeled.append((f"{i}）{seg.get('name') or '板块'}", seg.get("body", "")))
    for i, t in enumerate(c.get("special_topics") or [], 1):
        labeled.append((f"专题段{i}", t.get("body", "")))
    texts = [t for _n, t in labeled]
    seg_tbl = c.get("segment_table")
    if seg_tbl:
        for r in seg_tbl.get("rows") or []:
            texts.append(" ".join(str(x) for x in r))

    # 模型在 data_notes 里声明过的推算值，单独归为「已声明推算」，不算未溯源
    declared: set[str] = set()
    for note in (draft.get("data_notes") or []):
        for n in collect_numbers(str(note)):
            digits = re.sub(r"[^\d.]", "", n)
            if digits:
                declared.add(digits)
                try:
                    declared.add(f"{float(digits):.2f}")
                except ValueError:
                    pass

    unsourced: list[dict] = []
    derived: list[dict] = []
    seen_num: set[str] = set()
    for label, text in labeled:
        for sent in re.split(r"(?<=[。！？])", text or ""):
            for n in collect_numbers(sent):
                digits = re.sub(r"[^\d.]", "", n)
                try:
                    v = float(digits)
                except ValueError:
                    continue
                if digits in corpus_nums or f"{v:.2f}" in corpus_nums or f"{v:.1f}" in corpus_nums:
                    continue
                key = f"{label}|{n}"
                if key in seen_num:
                    continue
                seen_num.add(key)
                hit = {"num": n, "where": label, "sent": _cite(sent, n),
                       "kind": _guess_kind(sent, n)}
                (derived if (digits in declared or f"{v:.2f}" in declared) else unsourced).append(hit)

    if unsourced:
        lines = [f"以下 {len(unsourced)} 个数字在材料原文里找不到直接出处，多为正文自算的占比、增速或差额，请复核算式："]
        for h in unsourced[:12]:
            kind = f"（疑似{h['kind']}）" if h["kind"] else ""
            lines.append(f"· {h['num']}{kind} —— {h['where']}：「{h['sent']}」")
        if len(unsourced) > 12:
            lines.append(f"……另有 {len(unsourced) - 12} 处")
        issues.append("\n".join(lines))
    if derived:
        lines = [f"以下 {len(derived)} 个数字 data_notes 已声明为推算值，核对注释是否写清口径："]
        for h in derived[:8]:
            lines.append(f"· {h['num']} —— {h['where']}：「{h['sent']}」")
        issues.append("\n".join(lines))

    # 3) 期次写法校验：防止把「中报」写成「9月末／1-9月」
    def _norm_period(s: str) -> str:
        s = re.sub(r"\s+", "", s or "")
        return s.replace("—", "-").replace("－", "-").replace("～", "-")

    labels = period_labels(facts)
    pat = re.compile(r"20\d{2}年(?:\d{1,2}月末|\d{1,2}-\d{1,2}月)")
    allowed = {_norm_period(w) for lab in labels.values() for w in lab.values()}
    years = {m.group(0) for w in allowed for m in [re.match(r"20\d{2}", w)] if m}
    # 材料原文里本就出现的期次（如评级报告写「2025年3月末」）不算编造
    for mat in materials:
        for m in pat.finditer(_norm_period(mat.get("text") or "")):
            allowed.add(m.group(0))
    bad = {p for t in texts
           for p in (m.group(0) for m in pat.finditer(_norm_period(t or "")))
           if p not in allowed and p[:4] in years}
    if bad:
        issues.append("以下期次写法在台账与材料原文中找不到依据（可能是把材料里不同期次的数字拼到了同一个期次上），需核对：" + "、".join(sorted(bad)))

    # 4) 财务表自洽：资产 = 负债 + 权益，差额多来自台账字段错位
    ft = (c.get("financial_table") or {})
    rows = {r[0]: r[1:] for r in (ft.get("rows") or []) if r and r[0]}
    if all(k in rows for k in ("资产总额", "负债总额", "所有者权益")):
        for i, per in enumerate(ft.get("periods") or []):
            try:
                a = float(rows["资产总额"][i].replace(",", ""))
                l = float(rows["负债总额"][i].replace(",", ""))
                e = float(rows["所有者权益"][i].replace(",", ""))
            except (ValueError, IndexError):
                continue
            if abs(a - l - e) > 0.05:
                issues.append(f"{per} 财务表中资产总额与负债加权益相差 {a - l - e:+.2f} 亿元，需回台账核对该期字段")

    # 5) 「未披露」与材料对不上：材料里有该年度报表，正文却整列写未披露
    miss_cols = []
    for i, per in enumerate(ft.get("periods") or []):
        vals = [str(r[i]) for r in (ft.get("rows") or []) if len(r) > i]
        if sum(1 for v in vals if "未披露" in v or not v.strip()) >= 2:
            miss_cols.append((i, per))
    if miss_cols:
        blob = "\n".join(m.get("text") or "" for m in materials)
        has_stmt = any(a in blob for a in ("资产总计", "资产合计", "资产总额"))
        hard = [per for _i, per in miss_cols
                if re.sub(r"[^\d]", "", per)[:4] in blob and has_stmt]
        if hard:
            issues.append("财务表 " + "、".join(hard) + " 整列写「未披露」，但材料原文含该年度报表"
                          "（资产总计/资产总额），多半是摘录漏页或漏读，需回材料核对")
        else:
            issues.append("财务表 " + "、".join(per for _i, per in miss_cols)
                          + " 有整列空缺，确认材料确实未披露该期次")

    # 6) 叙述视角残留：程序已清理过一轮，还漏的就是句子结构太绕
    left = [t for t in texts if has_view_words(t or "")]
    if left:
        sample = "；".join(_sketch(t) for t in left[:3])
        issues.append(f"正文仍有 {len(left)} 处交代资料来源的表述（程序未能自动改写），"
                      f"建议改为直接陈述或删去：{sample}")

    # 7) 跨段重复：同一句话在别处已经说过。只提示不自动删——
    # 业务段的风险点与专题段的核心证据常常是同一事实，机器分不清该留哪处。
    named: list[tuple[str, str]] = [("基本情况", draft.get("basic_info") or "")]
    named.append(("总述段", (c.get("overview") or {}).get("body") or ""))
    for i, s in enumerate(c.get("segments") or [], 1):
        named.append((f"{i}）{s.get('name') or '板块'}", s.get("body") or ""))
    named.append(("资产段", (c.get("asset") or {}).get("body") or ""))
    named.append(("债务段", (c.get("debt") or {}).get("body") or ""))
    for i, t in enumerate(c.get("special_topics") or [], 1):
        named.append((f"专题段{i}", t.get("body") or ""))
    seen_sent: list[tuple[str, str]] = []
    dup_hits: list[str] = []
    for label, text in named:
        for sent in re.split(r"(?<=[。！？])", text):
            core = _core_text(sent)
            if len(core) < 20:
                continue
            for prev_label, prev_core in seen_sent:
                if is_real_dup(core, prev_core, 18, min_han=10):
                    dup_hits.append(f"「{sent.strip()[:26]}…」在{prev_label}与{label}重复")
                    break
            else:
                seen_sent.append((label, core))
    if dup_hits:
        issues.append("同一表述在两处出现，删一处即可：" + "；".join(dup_hits[:3]))

    # 8) 程度词与过短板块：程度词留给数据去说，板块段要有实质内容
    vague = sorted({w for w in ("极大", "极强", "极弱", "恶劣", "严重恶化", "显著")
                    for t in texts if w in (t or "")})
    if vague:
        issues.append("正文含不可核实的程度词 " + "、".join(vague) + "，建议换成数据或分级判断词")
    thin = [(s.get("name"), len((s.get("body") or "").strip()))
            for s in (c.get("segments") or []) if 0 < len((s.get("body") or "").strip()) < 120]
    if thin:
        issues.append("以下板块段内容偏薄，补一句经营数据或风险点，或与同类小板块并为一段："
                      + "、".join(f"{n}（{k} 字）" for n, k in thin))

    # 8) 结构检查
    if len(draft.get("positive") or []) < 3:
        issues.append("正面不足 3 条")
    if len(draft.get("concern") or []) < 3:
        issues.append("关注不足 3 条")
    if not (c.get("overview") or {}).get("lead"):
        issues.append("总述段缺加粗汇总句")
    if not (c.get("financial_table") or {}).get("rows"):
        issues.append("财务指标表为空（台账与材料里都没找到可用的财务数据）")

    body_len = sum(len(t) for t in texts)
    if body_len < 2000:
        issues.append(f"正文偏短（约{body_len}字），模板要求 2000-4000 字")
    if body_len > 5200:
        issues.append(f"正文偏长（约{body_len}字），建议压缩到 4000 字以内")

    return {"issues": issues, "body_chars": body_len}


def generate(facts: dict, materials: list[dict], model_id: str | None = None,
             progress=None) -> dict:
    import threading
    import time

    _p(progress, 3, "组装提示词", "合并台账与材料原文")
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(facts, materials)},
    ]
    chars = sum(len(m.get("text") or "") for m in materials)
    _p(progress, 8, "模型写作中", f"已送入 {len(materials)} 份材料、约 {chars // 1000}k 字")

    stop = threading.Event()

    def tick():
        t0 = time.time()
        while not stop.wait(1.5):
            e = time.time() - t0
            # 逐步逼近 88%：模型实际耗时不可知，进度只作参考，不做承诺
            pct = 8 + int(80 * (1 - 2.718281828 ** (-e / 75)))
            _p(progress, pct, "模型写作中", f"已用时 {int(e)} 秒")

    th = threading.Thread(target=tick, daemon=True)
    th.start()
    t0 = time.time()
    used: dict = {}
    try:
        raw = llm.chat(messages, model_id=model_id, temperature=0.3, max_tokens=12000,
                       timeout=900, thinking=False, usage_out=used)
    except Exception as e:
        # 失败也记：token 可能已经消耗（超时、空返回），不记会让统计偏低
        usage_mod.record("generate", company=facts.get("company", {}).get("name") or "",
                         model=used.get("model") or (model_id or ""),
                         prompt_tokens=used.get("prompt_tokens", 0),
                         completion_tokens=used.get("completion_tokens", 0),
                         total_tokens=used.get("total_tokens", 0),
                         cached_tokens=used.get("cached_tokens", 0),
                         seconds=time.time() - t0, chars=chars, ok=False,
                         estimated=bool(used.get("estimated")),
                         note=f"失败：{e}"[:150])
        raise
    finally:
        stop.set()

    secs = time.time() - t0

    _p(progress, 90, "解析结构", "检查 JSON 与字段完整性")
    draft = llm.parse_json(raw)
    if isinstance(draft, list):
        draft = draft[0] if draft else {}
    draft = _ensure_structure(draft, facts)
    _p(progress, 96, "校验", "数字溯源、期次写法、财务表自洽")
    draft["validation"] = validate(draft, facts, materials)

    # 记一行用量：主体名取草稿里的全称，便于回看「哪家花了多少」
    usage_mod.record("generate",
              company=(draft.get("company_full_name")
                       or facts.get("company", {}).get("name") or ""),
              model=used.get("model") or (model_id or ""),
              prompt_tokens=used.get("prompt_tokens", 0),
              completion_tokens=used.get("completion_tokens", 0),
              total_tokens=used.get("total_tokens", 0),
              cached_tokens=used.get("cached_tokens", 0),
              seconds=secs, chars=len(raw), ok=True,
              estimated=bool(used.get("estimated")),
              note=f"{len(materials)} 份材料 · 正文 {draft['validation'].get('body_chars', 0)} 字")
    draft["usage"] = {
        "model": used.get("model") or (model_id or ""),
        "prompt_tokens": used.get("prompt_tokens", 0),
        "completion_tokens": used.get("completion_tokens", 0),
        "total_tokens": used.get("total_tokens", 0),
        "cached_tokens": used.get("cached_tokens", 0),
        "calls": used.get("calls", 0),
        "seconds": round(secs, 1),
        "estimated": bool(used.get("estimated")),
    }
    return draft
