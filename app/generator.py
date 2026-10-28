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


def _period_key(label: str) -> tuple[int, int]:
    """把期次写法换算成可比较的键：先年、再月。

    财主要指标表的期次写法就那几种：「2024年」「2026年3月末」「2026年1-9月」
    「2025年6月末」。取年份与「截至月」，区间取期末月；认不出的按年中排，
    保证不会因为一个怪写法把整张表打乱。
    """
    t = (label or "").strip()
    y = re.search(r"(\d{4})\s*年", t) or re.search(r"^(\d{4})$", t)
    year = int(y.group(1)) if y else 0
    # 期末月：3月末 / 6月末 / 9月末 / 12月末；区间 1-3月 取 3
    m = re.search(r"(\d{1,2})\s*月末", t)
    if m:
        month = int(m.group(1))
    else:
        m = re.search(r"1\s*-\s*(\d{1,2})\s*月", t)
        if m:
            month = int(m.group(1))
        elif re.search(r"年度?$", t):
            month = 12
        else:
            month = 6
    return (year, month)


def sort_periods_desc(labels: list[str]) -> list[str]:
    """新一期在左、旧一期在右。评级报告的表一律这样排，程序统一兜住，
    不指望每个模型每次都记得。"""
    return sorted(labels or [], key=_period_key, reverse=True)


def reorder_table(table: dict) -> dict:
    """把财务表／业务表的期次列改成「最近一期在最左」，数值列跟着一起换位。

    只动列顺序，不动行与内容：periods 与每行的 values 用同一套下标重排，
    所以不会出现「表头换了、数字没换」的错位。
    """
    if not isinstance(table, dict):
        return table
    periods = list(table.get("periods") or [])
    if len(periods) < 2:
        return table
    order = sorted(range(len(periods)), key=lambda i: _period_key(periods[i]), reverse=True)
    if order == list(range(len(periods))):
        return table
    out = dict(table)
    out["periods"] = [periods[i] for i in order]
    rows = []
    for r in table.get("rows") or []:
        r = list(r)
        if not r:
            rows.append(r)
            continue
        head, rest = r[:1], r[1:]
        # rest 比 periods 短时（缺列的旧数据）按原样补齐，宁可不排序也不越界
        if len(rest) == len(periods):
            rows.append(head + [rest[i] for i in order])
        else:
            rows.append(r)
    out["rows"] = rows
    return out


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
    # 先按新→旧排再取前三：台账若按时间正序给，直接 [:3] 会拿到最早三期
    periods = sort_periods_desc([p for p in (fin.get("periods") or []) if p])[:3]
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

【体例】报告只有两层：摘要层（正面：/关注：各3条，最多5条，编号 1）2）3））与正文层（1、基本情况；2、行业情况；3、点评）。
点评由以下块按序组成：总述段 → 业务构成表 → 分部业务段（1）2）…）→ 资产段 → 财务指标表 → 盈利段 → 债务段 → 或有/专题段（可选）。

【硬规则】
1. 摘要层是定性概括，除判断核心依赖量级外不得出现金额、百分比、倍数、占比。判定口径：删掉这个数字，这条判断是否仍成立？成立就删掉数字。
2. 摘要一条只讲一件事，正面与关注不得重复同一事实，不得互相矛盾。
3. 正文每段以一句加粗汇总句开篇（总述段、资产段、盈利段、债务段、专题段），汇总句带判断不带数字，且必须带主语（见 3.1）；分部业务段不加段首加粗。
3.1 主语写在生成时，不是事后补。正文（含加粗汇总句、各段正文）与摘要条目里，每一个判断句都要能回答「是谁」：主语用「公司」，需要点名身份时写「公司为……」「公司作为……」；涉及股东、政府、行业、政策时主语换成对应主体（如「中山市人民政府」「区域水泥需求」）。三种写法都不合格，生成时就别写出来：
　（1）主语脱落：「区域专营地位较强」「股权关系清晰稳定」「债务规模持续扩张，短期偿付压力上升」——读者不知道说的是谁，也不像评级机构对受评主体的判断。
　（2）主语飘移：前一分句的主语是公司，后一分句换了主语却当成同一件事继续说，例如「公司应收账款规模较大，回款依赖财政拨付，占用资金」——第三分句的主语变成了回款，读者会分不清被占用的是谁的资金。换主语就换分句、明写新主语。
　（3）拿属性当主语：「资产以应收与存货为主」可写（主语是资产，句子完整），但「资产质量一般，回收存在不确定性」不行——判断落在「资产质量」这个属性上，实际要说的是「公司这部分资产的回收能力」。
　反例：「区域专营地位较强，外部支持力度大。」
　正例：「公司为天心区政府全资持有的城市建设主体，区域专营地位较强，资产注入与政府补助构成外部支持的主要来源。」
　检验法：把这一句单独拎出来读，能不能答出「是谁」；答不出就补主语，而不是靠上下文让读者猜。
4. 金额统一亿元、两位小数、带千分位；比率两位小数加百分号；倍数两位小数。期次写明「2025年」「2026年9月末」「2026年1-9月」。**表格的期次列一律由新到旧排列，最近一期在最左、最旧的在最右**（与跟踪评级报告同款），不要按时间正序。原始表格多以万元计（也有以元、美元计的），先换算再写，不得把万元数直接当亿元；同段落里的量级要能相互印证——按总资产规模判断，货币资金、有息债务不可能是差着两三个数量级的数。
5. 不得虚构任何数字、项目、名称。材料里没有的信息就不写，不得推算、不得用行业均值替代；也不要在正文里交代「哪一项没拿到」——规则 9 的某一拍缺数据就整拍跳过，不要写成「××方面，材料未披露」。财务指标表单元格确实无值的填「—」。
6. 不得照抄评级机构的结论句作为自己的判断；可复算的要用自己的口径重述。不得出现「中诚信国际认为」「评级时应注意到」这类元话语。
7. 业务分部段六拍：业务模式与产出能力 → 报告期的产出量 → 价格或费率与收入 → 成本结构与毛利率变动原因 → 项目或订单储备 → 这一块受什么制约。产出量的计量单位按业务性质选（取该公司连续披露、可跨期比较、且与收入同向的那一个量），不同板块不必统一；找不到就跳过这一拍，不拿无关指标充数。单个板块的正文至少三句：做的是什么、当期经营数据、制约条件，各占一句以上；收入占比不足 5% 的板块并成一段「其他业务」写，不要为凑板块数量把一句话拆成一段。
7.1 最后一拍不是「点风险」，是把制约条件接在它已经影响的那件事后面。谁付钱、按什么结算、什么时候能回、价格由谁定、储备够不够，用「取决于」「受……制约」「一旦……将」把条件和后果连起来，让读者看到这件事怎么发生。禁止另起一句给它贴标签——「该板块风险在于……」「该板块风险点在于……」「本板块主要风险是……」「板块风险主要表现为……」这类引导语一律不写：贴了标签，后面半句往往只剩风险的名字，没有发生机制。「需关注……」也要带条件，只挂一个风险名称（「需关注合规风险」）等于没写。
检验法：把引导语删掉，剩下的半句若主语仍是具体条件（回款、去化、价差、期限、担保对象）且能独立成句，合格；若删掉就读不通、或主语还是「该板块」，说明这句话本来就是硬贴上去的。
正例：「……2025年1-3月确认收入0.88亿元，同期回款0.31亿元，已确认收入与已回款之间的缺口随结算进度波动，回款节奏取决于天心区财政资金安排。」
反例：「……2025年1-3月确认收入0.88亿元。该板块风险点在于回款依赖天心区财政结算，已确认收入与已回款存在缺口。」
三件事要连成一段叙述，不按这三件事分段，也不写成三个并列短句。
8. 资产段逐科目展开，一个科目一句话，句式固定为「科目 金额（占资产总额 ××.××%），主要为×××，再交代质量」：
   - 科目取前三大到前五大，按最新期次的实际金额降序排列（不是沿用旧名单换数字）。
   - 括号里只放占比，写法固定为「（占资产总额 27.74%）」；占比由该科目金额÷资产总额算出，并写进 data_notes。
   - 括号之后紧跟「主要为……」，把这个科目里装的是什么说清：对手方是谁、做什么用、什么形态（土地房产、往来款、工程结算款、理财与存单）。这句不是评价，是把科目名还原成事实。
   - 每个科目还要落到质量：什么时候能回来、在谁手里、有没有被质押冻结、账龄落在哪一档。
   - 段内另给一句前五大合计占资产总额的比重；受限资产收在段末，写清金额、占总资产比重、主要为哪些资产、因什么受限。
   - 反例：「应收账款规模较大，回收存在一定不确定性；存货占比较高，变现能力一般。」
   - 正例：「应收账款 342.16 亿元（占资产总额 27.74%），主要为应收××区财政局的工程结算款，账龄一年以内的占 68.20%，回款节奏取决于年度预算安排；存货 128.40 亿元（占资产总额 10.41%），主要为代建项目的开发成本，未计提跌价准备，变现依赖项目结算进度。」
   - 只写「规模较大」「占比较高」不写金额与占比，等于没写：读者判断不了这个科目到底压了多少资产。
9. 债务段按固定顺序展开：负债总额与资产负债率 → 全部债务及长短债结构与占比 → 有息债务的融资构成 → 利息保障与经营获现 → 非受限货币资金对短债覆盖 → 授信与未使用额度。或有事项不进债务段——它由用户在页面上自行决定是否单列一段（见规则 21），债务段里不写「或有事项方面，……」。
10. 全文用合并口径。总债务（含政府专项债等）与有息债务是两个口径，混用时必须说明差额来源。
11. 期次只能照抄台账『期次写法』里的字面写法：时点用「X年末／X年6月末／X年9月末」，区间用「X年／X年1-6月／X年1-9月」。资产类、负债类、债务类数据用时点写法，收入、利润、现金流用区间写法。台账写「中报」的，时点就是「X年6月末」，绝不允许写成 9 月末或 1-9 月。
12. 不按行业套路写作。写外部条件时问三件事：钱从谁那里来、量由什么决定、价由谁定；三问的答案决定引什么数据。不要因为主体名称或行业标签就推断它应该有什么业务、什么风险——只写材料里有据、且该主体确实存在的事实。找不到某项惯常指标（例如某类产出量），就跳过，不用相近指标替代。
12.1 行业情况单列一段（industry），位置在基本情况之后、点评总述段之前，是独立的一节，不和公司自身经营混在一起写。这一段只回答「量由什么决定、价由谁定」，四件事各一到两句，按对公司的传导强弱排序，不按行业报告的目录顺序：
　① 供需格局：行业所处阶段、产量或需求增速、产能利用率、需求端由哪几块构成（基建、地产、制造业投资……）；
　② 价格与成本：产品价格或价格指数的走势与变动幅度、主要原材料与能源成本的变化；
　③ 政策：产能置换、环保限产、补贴退坡、定价与结算机制中真正影响这家公司的那一条；
　④ 传导：前三件事怎样落到公司的量、价、毛利上，用一句话接住，转下一节。
　写法要求：每个判断后面跟一个可核实的数（增速、幅度、利用率、区域投资额）；材料只给定性表述（「行业景气度下行」）时，改写到公司自己的数据上（公司产量降了多少、均价降了多少），不拿行业套话充数。不列行业风险清单——「行业竞争加剧」「政策存在不确定性」这类没有对象、也没有影响方向的句子一律不写；凡删掉后读者仍说不出「这件事影响公司哪个指标、往哪个方向」的句子，都不写。
　与总述段分工：行业的量、价、政策在行业段写完，总述段只写公司自己的收入结构、板块拆分与盈利主线，行业数据不在两段各写一遍（见 19）。
12.2 基本情况必须交代注册资本与实收资本：写清注册资本多少、实收资本多少；两者不一致时要说明差额是什么（未实缴、分期到位、口径不同），不能只写其中一个。股东及持股比例只写当前状态与实际控制人；报告期内股权、控制权没有变化的，不写「未发生变化」（见 19.2）。
13. 叙述视角：报告是评级机构对受评主体的判断，句中主语只能是公司、股东、政府、行业或政策，不能是「材料」。全文禁止出现「材料」「材料原文」「公开资料」「公开信息」「本报告」「本文」，禁止出现「披露」「未披露」「载明」「列示」「数据显示」「报表显示」这类转述来源的词，也禁止「从外部条件看」「从内部条件看」「从财务表现看」这类框架提示语——直接陈述事实与因果。判断标准：把这些词删掉后句子依然成立，就说明本来不该写。
14. 正文不出现编制层面的说明：不写「因数据可得性」「受限于资料」「以××为准」「详见下表」「如下所示」。表格由程序排版，正文只管判断。
15. 概念不得混用：已实现营业收入不等于合同金额、中标金额或订单储备；净利润不等于 EBITDA，也不等于经营活动现金流；年度数据不等于单季或累计数据；历史数据不等于预测数。用之前先确认是哪一种，口径不同时在句中点明。
16. 变化要算幅度并归因：收入、利润、毛利率、资产负债率、有息债务等关键指标，与上期相比变动超过 5% 的，写出变动幅度并说明成因（量、价、结构、并表范围、政府补助、会计口径调整），不要只并列两期数字让读者自己算。
16.1 自算值必须交底算式。凡正文里出现的、不是材料直接给出的数（占比、增速、变动幅度、倍数、差额、合计），逐条写进 data_notes，一行一条，格式固定为：
指标名 = 操作数A 运算符 操作数B [运算符 操作数C]
例：
- 短期债务占比 = 3,421.53 ÷ 5,924.25 × 100%
- 毛利率同比上升 = 73.84 − 67.44
- 资产负债率 = 9,121.40 ÷ 13,337.76 × 100%
操作数一律用材料里的原值（不要写成「上年数」「总额」这种代称），单位统一亿元或百分比。程序会按这些算式逐条复算：算得上就不用人工核，算不上的会挑出来问。没有列进 data_notes 的自算值，一律按「未交底」对待、进待核对清单，所以宁可多写一行，不要漏。
材料直接给出、只需照抄的数（资产总额、营业收入、债券余额等）不用写算式。
17. 措辞分级、不用极端词与宣传语：判断词只用很强／较强／一般／较弱、可控／充足／尚可／承压这类分级表述，不用「极强」「极弱」「恶劣」「极大」「严重恶化」；材料里的「荣获」「行业领先」「填补空白」「有力保障」「成效显著」等宣传语与美化表述一律不进正文，只写可核实的事实与风险。
18. 正文每段至少锚定一个具体数据或明确事实（金额、比率、期限、数量、协议条款），不写通篇定性的空段；也不为凑数据引与判断无关的数字——引的每个数都要能用在一句判断上。
19. 同一件事只说一遍。加粗汇总句是判断，正文不得把这句话换个说法再说一次；专题段只写它自己那件事的新证据，不复述债务段、资产段或业务段已经写过的数据与结论；同一条事实（如某科目余额、某协议条款）在全文只出现一次，后文需要引用时用「上述」带过或直接给结论，不重抄原句。
19.1 总述段与盈利段的分工写在纸上，不许两段各写一遍：总述段给「收入是多少、由哪几块构成、利润率朝哪个方向走」，末尾一句点明赚钱还是亏钱；盈利段从「降在哪一块、是量还是价」写起，再展开利润与亏损归因。营业收入的总数、各板块收入与占比，总述段写过之后盈利段不再重抄——要提就写「上述收入结构下」「其中基础建材板块」这种承接，不要第二次列出整串金额。反过来，减值提了多少、费用为什么变，只在盈利段说，总述段不要提前把归因讲完。
19.2 没有变化的事不写。报告只记录变化和它的影响，不记录「没变」：控股股东及持股比例未发生变化、外部主体评级维持原有等级且展望未调整、审计机构未更换、注册地址未搬迁，这些都不写。与偿债能力无关的人事变动也不写——董事长、总经理、董事的任免与更替，内部部门调整，都属于这一类。
　判断方法：把这件事写成一条公告，读者看了会不会改变「这家公司能不能按期还钱」的判断？不会，就整句删掉。不要因为它出现在材料里、或者是跟踪报告固定栏目，就顺手抄进来占字数。
　反过来，这些必须写，而且要写出对信用水平的影响方向：控股股东或实际控制人变更为风险主体、控制权发生转移、主体评级下调或列入观察名单、评级展望调为负面、审计意见类型变化、会计师事务所因故更换并出具非标意见。
　正例：不写「跟踪期内公司控股股东及持股比例未发生变化。董事长由王彤宙变更为宋海良，总经理由王海怀变更为张炳南。」——两句都不影响偿债判断，整段删掉。
　正例：要写「2026年3月公司控股股东由××市国资委变更为××控股集团，持股比例由100%降至51%，实际控制人未变，政府支持路径改由新股东归口，需关注后续注资与结算安排。」——变化本身带有信用含义。
20. 分部业务段是一整段：编号与板块名之后的全部内容在同一段里连续写完，不在段内换行、不分成若干小段、不加小标题。
21. 摘要从正文倒抽，不从材料正推。写完正文后再回头逐段读，把「能独立成立、且对偿债有含义」的判断句圈出来当候选池：先圈 10-14 条进 positive_pool / concern_pool（每条 30-50 字、纯定性、不带数字、不写「公司」开头以外的修饰），再从中按分量排序挑 3-5 条进 positive / concern。定稿的条目必须同时出现在候选池里。候选池里不要放正文没说过的话——它只是正文的抽提，不是第二遍分析。
21.1 摘要层只分等级，不做评价。程度词只用有共同刻度的那些：很强／较强／一般／较弱（能力、地位、支持力度、专营性），充足／尚可／承压／不足（流动性、覆盖、再融资），偏高／合理（占比、杠杆、集中度），顺畅／受限（融资渠道、回款），清晰／待理顺（股权、治理）。没有刻度的词一律不写：显著、突出、明显、良好、较好、优良、充裕、雄厚、强劲、领先、龙头、重要增长极、核心平台、有力、有效、大幅、充分、完善、稳健、向好、稳步。理由不是这些词不好听，而是它们没有共同刻度——同一件事，换个人写就会落在「较强」或「一般」，读者无法据此比较两家公司。也不写「存在一定风险」「债务期限结构有待优化」「仍存××风险」这类委婉收尾：把「有待优化」还原成它实际指向的事（短债占比偏高、非受限资金覆盖不足、到期集中）。
21.2 每条只讲一件事，一句话，30-50 字。正面写「支撑从哪来 → 因此形成什么程度的优势」，关注写「哪个环节出问题 → 影响到哪一类偿付」。条目内不出现分号，不出现「同时／此外／且」接第二件事；出现就拆成两条。同一指标不得在正面与关注里各说一次；几条正面也不得用不同说法重复同一句判断。
反例：「公司规模优势突出，财务弹性充足，发展前景广阔。」
正例：「水泥、混凝土与石膏板产能分布于全国二十余个省份，单一区域需求下滑对收入的冲击相对可控。」
反例：「公司债务规模持续扩张，短期债务占比较高，债务期限结构有待优化。」
正例：「有息债务连续三年增长，短期债务占比偏高，非受限货币资金不足以覆盖一年内到期部分。」
21.3 主语同正文 3.1，一条不例外：每条都要能回答「是谁」，主语用「公司」，需要点名身份时写成「公司为……」或「公司作为……」。摘要比正文更容易脱落主语——它从正文里抽句，抽的时候顺手把主语删掉了，就成了「区域专营地位较强」「股权关系清晰稳定」这种没有主体的判断。
反例：「区域专营地位较强，外部支持力度大。」
正例：「公司为天心区政府全资持有的城市建设主体，区域专营地位较强，资产注入与政府补助构成外部支持的主要来源。」
主语不必每条都重复堆在句首：前一条已明确是公司，后一条可以承接省略；但只要这条单独拎出来读不出主体，就要补上。
22. 专题段默认空数组，但「值得单列的事项」不能因为它没被写进正文就消失。
22.1 special_topics 默认输出空数组：加不加段由用户在前端决定，不必为了完整而补。确有非报不可的重大事项（大额对外担保且被担保方已出现风险、重大未决诉讼、股东出资未到位、在建项目投资缺口远超货币资金）才写，最多一条，必须带独立的加粗判断句作 lead，body 再摆数据；不要用「或有事项方面，」这种引子开头。
22.2 另一头，分析中发现「够不上专题段、但值得单列一段」的事项，写进 add_on_sections——它不是正文，是给用户的提醒：这一事项可以在模板的哪个位置补一段。必须逐项扫这六类，扫到就写，扫不到就空着：①对外担保（余额、占净资产比重、被担保方是谁、是否互保、是否已出现代偿）②未决诉讼与仲裁（涉案金额、进展、是否已计提）③对外承诺与票据保函（敞口、到期分布）④股东出资与注资进度（承诺额、到位额）⑤在建项目资金缺口（计划总投资、已累计投资、剩余投资、货币资金）⑥政府补助依赖（补助金额与净利润的比值）。
22.3 每条五个字段：where 只能写「资产段之后」或「债务段之后」；title 是这段叫什么（如「对外担保」「未决诉讼」）；why 是为什么值得补，必须带材料里的金额与比重，这条只给用户判断用、不进正文；lead 是可直接用的加粗判断句，不带数字；body 是可直接用的正文，带实数，写清对手方、期限与触发条件。
22.4 六类一项都没有就给空数组。不要用「公司治理结构完善」「未发现重大异常」这类没有数据支撑的话凑数。
19. 两类专项按条件触发，不按行业标签触发。（1）收入主要来自政府结算（土地整理、代建、特许经营、购买服务）的，总述段交代「钱从谁来」时就要带上区域财力的实数——地区 GDP 与增速、一般公共预算收入、政府性债务余额或债务率，各一句、写明年份，并点明公司在这套结算体系里的层级与回款路径；不要只写「区域财力持续增长」这种不带数的判断，也不要把区域情况另起一段展开。（2）在建工程占非流动资产比重较高的，写清计划总投资、已累计投资、剩余投资，并与货币资金对照说明缺口。材料没有这些数据就整段不写，不许用「预计」「大致」补位。
23. 盈利段（profit）单独成段，位置在财务指标表之后、债务段之前。收入与利润分开写，不许只写收入不写利润。
23.1 收入侧：本期营业收入与同比变动幅度；把变动拆成量、价、结构三块，每块落到具体板块或产品——哪个板块销量降了多少、哪个产品均价降了多少、哪个板块新并表或已退出。只写「营业收入下降」不写降在哪一块、是量还是价，等于没写。
23.2 利润侧：利润总额与净利润各写本期数与同比变动。由盈转亏、亏损扩大、亏损收窄都要明写「亏损」，不得用「利润承压」「盈利弱化」「有所下滑」「利润空间收窄」代替——「承压」是评论，不是利润表上的数。
23.3 亏损或利润大幅下滑必须逐项归因，一项一句，每句带金额或幅度，按对利润的影响从大到小排：毛利变动（量、价、成本各影响多少）→ 期间费用（销售、管理、研发、财务费用各是多少、为什么变）→ 减值损失（资产减值与信用减值分别是多少、计在哪个科目或哪项资产上、依据什么提的）→ 投资收益与公允价值变动 → 政府补助与其他收益 → 资产处置损益 → 所得税。写完回算一遍：各归因项的方向与规模要能对上利润的变动，加不起来就说明漏项或数字抄错了，回到材料里核，不许就这么交。
23.4 归因只能落在材料里有实数的项上。材料给了减值金额就写金额；只给了「计提减值」没给数，就写「计提了减值」并把它排到后面，不编比例、不编构成。绝不能用「受行业景气度影响」「受宏观环境影响」「市场竞争加剧」「成本上升」这类不带数的话充当原因——那不是分析，是把要解释的事换个说法。也不许写「其余部分未披露」「具体原因不详」：只写有据的项，剩下的不写。
23.5 段末收口落在「这一年的利润被什么吃掉了、下一期还吃不吃」：哪些是偶发（一次性减值、资产处置、补助清退），哪些会持续（售价下行、毛利率中枢下移、费用刚性），下一期是否重演。这一句是评级最需要的，不要省，也不要用「未来盈利有望改善」这种没有依据的话代替。
【盈利段的检验法】把段里每个归因句单独拎出来问：这句话里的金额在材料里能不能找到？把它删掉，利润下降这件事还解释得通吗？——找不到金额的是编的，删掉也照旧解释得通的是废话。
23.6 利润数字只能取材料的原值。合并净利润材料没给，就写「净利润为负」「由盈转亏」，用量级证据把亏损的规模撑起来（利润总额多少、同比降多少、减值提了多少），**不许用子公司净利润顶替合并数，不许用净资产收益率乘所有者权益倒算，也不许用「利润总额×(1−所得税率)」凑一个**——这些数看着像真的，但材料和算式都不支持，比空着更危险。
反例（材料原话只有「计提大额资产减值损失，导致净利润出现亏损」）：写成「净利润-8.39亿元、由上年盈利75.29亿元转为亏损」——两个数材料里都没有，8.39 是把 13.49 减 5.11 凑的，75.29 是子公司 75.28 抄错的。这种句子一经引用就要担责任。
正例：「2025年利润总额12.45亿元、同比下降87.05%，净利润为负、由盈转亏；当期资产减值损失68.17亿元、信用减值损失17.38亿元，较上年分别增加63.06亿元和13.43亿元，两项合计构成亏损的主要来源」——一个数都没编，亏损的规模也交代清楚了。
财务指标表里材料没给的项一律留「—」。确实需要自己算的（差额、合计、占比、增速），按 16.1 把算式写进 data_notes，一行一条，程序会复算；表格里找不到出处的数会被程序置空并提示，那不是程序太严，是这一格没人能证明它。
23.7 亏损归因的每一笔都要能对上账：减值提了多少、毛利变动多少、费用增减多少，加起来要能解释利润总额从多少掉到多少。差一个量级就说明还有一项没找到或抄错了——回到材料里把那一项找出来，找不到就不写，不要用「其他因素」兜底。
24. 材料的用途按类别分，不要一视同仁地摘抄。本次材料可能包含跟踪评级报告、募集说明书、审计报告（含经审计的财务报表）与其他公告，分工如下：
　24.1 跟踪评级报告是判断主线：业务模式、行业量价、资产构成、债务结构以它为准。
　24.2 审计报告不是用来复述的。它只有两个用途：① 财务指标表的数字与它对齐——审计报告的三表是最终口径，评级报告的表格与它不一致时以审计报告为准；② 审计意见类型是偿债判断的一部分——标准无保留意见不必写（见 19.2 没有变化的不写），出现保留意见、带强调事项段、无法表示意见、否定意见，或首次出现持续经营重大不确定性段落，必须在基本情况或债务段写出来，说明段落的原文事由（如「与持续经营相关的重大不确定性」涉及哪笔债务、哪项资产）。审计报告的附注、会计政策变更说明、报表项目明细一律不进正文。
　24.3 其他公告是用来找「报告期内发生了什么」的：诉讼与仲裁、担保代偿、监管处罚、评级下调、控制权与股权变动、重大资产处置、资金占用、债务展期或技术性违约。这一类只有出现对偿债有含义的事项才写，按 19.2 的判断法筛一遍——写成一条公告，读者会不会改变「这家公司能不能按期还钱」的判断？会，就写进对应的段（资产、债务或专题段），并带金额、对手方、时点；不会，就整句不写。程序性公告（付息、兑付、上市流通、发行结果、票面利率）即使被读进材料也不写。
　24.4 同一件事在多类材料里都出现时，只写一次，取最具体的那个数（审计报告与募集说明书里的同一科目余额不一致，以审计报告为准）。

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
    # 每份材料头标出日期与类别：类别决定模型怎么用它（规则 24），
    # 日期决定它以哪一版为准（同一年的审计报告可能有两个版本）
    mat_blocks = []
    for m in materials:
        if not m.get("text"):
            continue
        meta = "｜".join(x for x in ((m.get("date") or "").strip(),
                                     (m.get("kind") or "").strip()) if x)
        mat_blocks.append(f"===== {m['title']}" + (f"（{meta}）" if meta else "")
                          + f" =====\n{m['text']}")
    mat_block = "\n\n".join(mat_blocks)
    schema = """{
  "company_full_name": "工商全称",
  "positive": ["定稿的正面观点，3-5 条", "..."],
  "concern": ["定稿的关注观点，3-5 条", "..."],
  "positive_pool": ["候选：从正文回抽、够格但没进定稿的正面判断，5-8 条"],
  "concern_pool": ["候选：从正文回抽、够格但没进定稿的关注判断，5-8 条"],
  "basic_info": "150-300字，一段：成立与前身→主营与功能定位（含靠什么收钱）→注册资本与实收资本（两者不一致要说清差额原因）/股东及比例/实控人→增资到位、上市状态、并表子公司。报告期内股权、控制权、评级、审计机构没有变化的，一句都不写",
  "commentary": {
    "industry": {"lead": "行业段加粗判断句（不带数字，主语是行业或政策，如「水泥需求随基建与地产投资回落，价格中枢下移压制行业毛利」）", "body": "行业情况正文（独立一节，250-420字）：供需格局（产量或需求增速、产能利用率、需求端构成）→ 价格与成本（价格走势与幅度、原材料与能源成本）→ 政策（真正影响这家公司的那一条）→ 传导（怎样落到公司的量、价、毛利上）。每个判断后跟一个可核实的数"},
    "overview": {"lead": "加粗汇总句（不带数字）", "body": "总述段正文：先交代钱的来源与公司自身的经营主线，再写收入结构概览，收口引出业务构成表。行业的量价与政策已在行业段写过，这里不重抄"},
    "segments": [{"name": "该主体自己用的板块名，照原文写，不改写不带编号", "body": "承接『1）××业务方面，』之后的正文"}],
    "segment_table": {"unit_line": "单位：亿元", "header": ["业务板块", "营业收入", "收入占比", "毛利率"], "rows": [["板块名", "0.00", "0.00%", ""], ["营业总收入", "0.00", "100.00%", ""]]},
    "financial_table": {"unit_line": "单位：亿元", "periods": ["2023年", "2024年", "2025年"], "rows": [["资产总额", "0.00", "0.00", "0.00"], ["负债总额", "0.00", "0.00", "0.00"], ["净利润", "0.00", "0.00", "0.00"]]},
    "asset": {"lead": "加粗汇总句（不带数字）", "body": "资产段正文"},
    "profit": {"lead": "加粗汇总句（不带数字，说清这一年利润是被什么吃掉的）", "body": "盈利段正文：收入（量价结构）→ 利润总额与净利润及同比 → 亏损或下滑逐项归因（毛利、费用、减值、投资收益、补助、处置、所得税，每项带金额）→ 哪些偶发、哪些持续、下一期还吃不吃"},
    "debt": {"lead": "加粗汇总句（不带数字）", "body": "债务段正文"},
    "special_topics": [],
    "add_on_sections": [
      {"where": "债务段之后（或资产段之后）", "title": "这一段叫什么，如「对外担保」",
       "why": "为什么值得补一段：带材料里的金额与比重，这行只给用户看，不进正文",
       "lead": "可直接用的加粗判断句（不带数字）",
       "body": "可直接用的正文（带实数，写清对手方、期限与触发条件）"}
    ]
  },
  "data_notes": ["自算值的算式，一行一条，照此写：『比率·毛利率同比上升6.40个百分点 = 73.84 − 67.44』；口径差异或不确定处同样写在这里"],
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

注意：若【事实台账】已有「财务指标」，财务指标表由程序按台账生成，你不需要输出 financial_table；若台账没有财务指标，你必须从【材料原文】的「主要财务数据」表整理 financial_table——期次取最近三期（前两年 + 最新一期，按时间正序排列，最新一期放最后），期次写法照抄材料表头，行项按规模→结构→盈利→获现→债务→偿债排列，其中盈利类必须有「营业总收入」与「净利润」两行，材料给了「利润总额」「营业毛利率」也一并列出——利润表上看不出亏没亏，后面的归因就无从谈起；金额统一亿元、两位小数，数字与材料逐一对应，材料没有的行不要编。若台账无主营构成数据，请从评级报告正文补出 segment_table（金额按亿元、两位小数，占比两位小数，缺毛利率则该列留空字符串），否则 segment_table 输出 null。你只需保证正文里的数字与材料一致，且能被台账或材料原文支撑。segments 的 name 用该公司自己披露的板块名称，不要带「业务方面」四个字，也不带编号；板块数量按收入结构决定，不要为了凑段数拆分或合并材料里没有的板块。special_topics 默认给空数组（用户在前端按需加段）；确有重大事项才写一条，且必须自带加粗判断句作 lead。add_on_sections 是给用户的「可以在模板这里补一段」提醒，不要把它写进正文，也不要为了填满而凑数；扫到六类事项（对外担保、未决诉讼、对外承诺、股东出资、在建缺口、补助依赖）就给，扫不到给空数组。资产段每个科目都必须带金额与占比，并按 8 号规则的句式展开。"""


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

# 「该板块风险点在于……」这类点题引导语。
# 风险是长在事实后面的，不是另起一句贴的标签：贴了标签，后半句常常只剩风险的名字，
# 读者看不到它是怎么发生的。这里把引导语连同它前面的句号一起去掉、并成一个逗号，
# 让后半句直接接到已经说完的事实上（「……回款0.31亿元，回款节奏取决于财政安排」）。
# 只处理后面能接一整个小句的谓语；「风险是××」这类后半句多为名词短语，
# 「是」本身在承担谓语，删掉引导语就断了，所以不硬擦，交给校验提示去改。
_RISK_LEAD = re.compile(
    r"(?P<pre>[。；：，]|^)[ \t]*"
    r"(?:(?:该|本|上述|此|这一|该等)[ \t]*)?"
    r"(?:(?:板块|业务|分部|板块业务)[ \t]*的?[ \t]*)?"
    r"(?:主要)?[ \t]*风险(?:点)?[ \t]*(?:主要)?[ \t]*"
    r"(?:在于|来自|集中于|主要体现在|表现为|源自)[ \t]*[，,]?[ \t]*"
    r"(?=[^。；，\s])")


def _fuse_risk_lead(m: re.Match) -> str:
    """删掉点题引导语，把原来的句号/分号并成逗号，接上前一句。"""
    return "" if not (m.group("pre") or "") else "，"


# 校验用：只认「小句开头」的风险点题（「该板块主要风险是…」「风险在于…」）。
# 「公司应收账款的回收风险主要来自政府结算环节」这种把风险当定语的正常说法不算。
_RISK_TAG = re.compile(
    r"(?:^|[。；，])[ \t]*(?:(?:该|本|上述|此|这一|该等)[ \t]*)?"
    r"(?:(?:板块|业务|分部|板块业务)[ \t]*的?[ \t]*)?"
    r"(?:主要)?[ \t]*风险(?:点)?[ \t]*(?:主要)?[ \t]*"
    r"(?:在于|是|来自|集中于|主要体现在|表现为|源自)[ \t]*[，,]?")

# 摘要层（正面／关注）里没有刻度的评价词。
# 评级报告的程度词是有刻度的：很强／较强／一般／较弱、充足／尚可／承压／不足。
# 换成「显著」「突出」「良好」「充裕」，同一件事换个人写就变成另一档，读者无法比较，
# 也读不出到底强到什么程度——这是「不严谨」的来源，不是文风问题。
# 只收明确该换的词；「偏高」「集中」「尚可」这类有刻度的判断不在其中。
_HYPE_WORDS = (
    "显著", "突出", "明显", "良好", "优良", "优异", "卓越", "强劲", "雄厚",
    "充裕", "宽裕", "高企", "领先", "龙头", "标杆", "重要增长极", "核心平台",
    "核心枢纽", "有力", "有效", "大幅", "极大", "巨大", "广阔", "深厚", "一流",
    "充分", "完善", "稳健", "韧性", "较好", "向好", "稳步", "日益", "不断",
)
# 委婉收尾与模糊限定：读起来像判断，实际没说什么
_HYPE_PAT = re.compile(r"存在一定|有一定|一定的?(?:或有)?风险|仍存|有待|尚需|亟待|"
                       r"(?:能力|质量|表现|水平|环境|基础|弹性)(?:较|更)?好")

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

    三步：先把「该板块风险点在于」这类点题引导语融进前句，
    再擦掉「材料显示」这类前缀——绝大多数情况擦完句子本身就成立；
    擦不干净的（「××方面，材料未披露。」）最后按小句摘除。
    """
    t = (text or "").strip()
    if not t:
        return t
    t = _RISK_LEAD.sub(_fuse_risk_lead, t)
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


# ── 「可以在这里补一段」：模型没给时的兜底 ───────────────────────────────
# 规则 22 要求模型把担保、诉讼、在建缺口这类事项写进 add_on_sections，
# 但小模型经常把这个选填数组整个省掉（实测 deepseek-v4-flash 全空），
# 用户就看不到「这里能补一段」的提醒。这里退一步：材料里能摘到原句，
# 就做成一条提醒卡，正文留空由人改写——不虚构、不代写、也不擅自加段。
_ADV_RULES = (
    ("对外担保", ("对外担保", "担保余额"), ("无对外担保", "担保余额/"), "债务段之后"),
    ("未决诉讼", ("未决诉讼", "仲裁事项"), (), "债务段之后"),
    ("在建项目与资本支出", ("计划总投资", "剩余投资", "尚需投资"), (), "债务段之后"),
    ("股东出资", ("出资未到位", "尚未实缴", "承诺出资"), (), "资产段之后"),
)


def _advice_sentence(text: str, words: tuple[str, ...], excludes: tuple[str, ...],
                     max_len: int = 150) -> str:
    """在材料里摘一句「有实数、说这件事」的原文。

    PDF 抽出来的文本按版面换行，一句话常被切成几段，所以先把空白全部去掉
    再按句读定位；以关键词为锚向前找句首、向后找句末，避免摘出「要补充；公司
    固定资产……」这种从半句开始的碎片。
    """
    flat = re.sub(r"\s+", "", text or "")
    for need_amount in (True, False):
        for w in words:
            for m in re.finditer(w, flat):
                start = max(flat.rfind(c, 0, m.start()) for c in "。；") + 1
                ends = [x for x in (flat.find(c, m.start()) for c in "。；") if x > 0]
                end = min(ends) if ends else len(flat)
                s = flat[start:end]
                if any(x in s for x in excludes):
                    continue
                # 财务指标公式表里也会出现「担保余额/所有者权益×100%」，那不是事项
                if "×100%" in s or "计算公式" in s:
                    continue
                if not re.search(r"\d", s):
                    continue
                # 先找带金额的句子：只说「规模有所下降」的定性句不值得提醒补段
                if need_amount and not re.search(r"\d+(?:\.\d+)?(?:亿|万)元", s):
                    continue
                if len(s) < 15:
                    continue
                if len(s) > max_len:
                    # 在逗号处断开，别把半句话留在卡片上
                    cut = max(s.rfind(c, 0, max_len) for c in "，、；")
                    s = (s[:cut] if cut > 60 else s[:max_len]).rstrip("，、；") + "……"
                if len(s) < 15:
                    continue
                return s
    return ""


def fallback_advice(materials: list[dict]) -> list[dict]:
    out: list[dict] = []
    for title, words, excludes, where in _ADV_RULES:
        for m in materials:
            s = _advice_sentence(m.get("text") or "", words, excludes)
            if not s:
                continue
            out.append({
                "where": where, "title": title,
                # 摘出来的原句放 body，why 只说明来历——同一句话不要在卡片上出现两遍。
                # 这句也不能过 polish_language：那一层会删掉「材料」「披露」这类词，
                # 「材料原文」会被改写成「未改写）」这样的残句。
                "why": "原文里有这一节可写，这里是程序摘出的那句作起点；"
                       "要不要补、按什么语体重写，由你定。",
                "lead": "", "body": s, "auto": True,
            })
            break
    return out[:3]


def merge_advice(given: list[dict], materials: list[dict]) -> list[dict]:
    """模型给的建议优先；它没提到的类别，用材料摘句补一条提醒。"""
    rows = [a for a in (given or []) if isinstance(a, dict) and a.get("title")]
    have = " ".join(f"{a.get('title')} {a.get('body')} {a.get('why')}" for a in rows)
    for a in fallback_advice(materials):
        # 正文里已经写过这件事，就不用再提醒补段
        if any(k in have for k in (a["title"],)):
            continue
        rows.append(a)
    return rows[:5]


def _clean_advice(raw) -> list[dict]:
    """「可补充段落」建议的清洗。

    where 归一到模板里真实存在的两个落点——模型偶尔会写「第三章之后」
    「财务指标表之前」这种页面上指不到的地址，直接显示只会让人找不着。
    """
    advice: list[dict] = []
    for a in (raw or [])[:5]:
        if not isinstance(a, dict):
            continue
        title = str(a.get("title") or "").strip()
        # 程序摘录的原句保持原样：语言清理会删掉「材料」「披露」，把原文改残，
        # 而这张卡片的用途正是让用户看到材料里到底怎么写的。
        if a.get("auto"):
            body = str(a.get("body") or "").strip()
            why = str(a.get("why") or "").strip()
        else:
            body = polish_language(str(a.get("body") or "").strip())
            why = polish_language(str(a.get("why") or "").strip())
        if not title or not (body or why):
            continue
        advice.append({
            "where": "资产段之后" if "资产" in str(a.get("where") or "") else "债务段之后",
            "title": title[:20],
            "why": why[:220],
            "lead": _lead(a.get("lead")),
            "body": body,
            **({"auto": True} if a.get("auto") else {}),
        })
    return advice


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
    ind = c.get("industry") or {}
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

    industry = para(ind)
    overview = para(ov)
    segments = []
    for s in (c.get("segments") or []):
        if not str(s.get("body") or "").strip():
            continue
        nm = clean_segment_name(str(s.get("name") or ""))
        b = polish_language(clean_segment_body(nm, str(s.get("body") or "")))
        segments.append({"name": nm, "body": dedupe_sentences(b)})
    asset = para(at)
    pf = para(c.get("profit"))
    debt = para(db)
    topics = [para(t) for t in (c.get("special_topics") or []) if str(t.get("body") or "").strip()]

    out["commentary"] = {
        "industry": industry,
        "overview": overview,
        "segment_table": build_segment_table(facts) or _clean_llm_table(c.get("segment_table")),
        "segments": segments,
        "asset": asset,
        "financial_table": fin_table,
        "profit": pf,
        "debt": debt,
        "special_topics": [t for t in topics if t["body"]],
    }
    out["data_notes"] = draft.get("data_notes") or []
    out["sources"] = draft.get("sources") or []
    # 「可补充段落」建议：只给前端提示用，不并入正文。
    out["add_on_sections"] = _clean_advice(draft.get("add_on_sections"))
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

    for k in ("industry", "overview", "asset", "profit", "debt"):
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
    # 期次列统一改成「最近一期在最左」。放在这里而不是只写在提示词里：
    # 模型十次有两次仍按时间正序给，而错位很难在两千字里被肉眼发现，
    # 程序按同一套下标重排表头与数值，成本几乎为零。
    for key in ("financial_table", "segment_table"):
        if isinstance(c.get(key), dict):
            c[key] = reorder_table(c[key])
    d["commentary"] = c
    # 建议段只做语言清理（去「材料未披露」这类串场话），不改它的定位与去留：
    # 加不加由用户在页面上点，程序不替人决定。
    if isinstance(d.get("add_on_sections"), list):
        d["add_on_sections"] = [
            dict(a, lead=_lead(a.get("lead")), body=_pol(a.get("body") or ""),
                 why=_pol(a.get("why") or ""))
            for a in d["add_on_sections"] if isinstance(a, dict)]
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


# ── 机器自动核对：正文里自算的数，能不能用同段数据复原出来 ────────────────
# 待核对里原来有一大堆「12.34% 找不到出处」，其实多数是占比、增速、差额——
# 运算过程就在同一段里摆着（分子分母、两期数），机器自己算一遍就能定论，
# 不该甩给人。这里只做一件事：用同段里可溯源的数两两组合，把 target 复原出来。
# 复现不了才留在待核对里，并且带上「在第几段第几句」的位置。
_NUM_TOL = 0.02          # 两位小数四舍五入后的最大偏差


def _close(a: float, b: float) -> bool:
    """两个数是否在两位小数的精度内相等（大数放宽相对误差）。"""
    return abs(a - b) <= max(_NUM_TOL, abs(b) * 0.0016)


def _sentences(text: str) -> list[str]:
    """按句号切句，保留句子本身（不截断），用于定位到具体哪一句。"""
    out: list[str] = []
    start = 0
    for m in re.finditer(r"[。！？]", text or ""):
        s = (text[start:m.end()] or "").strip()
        if s:
            out.append(s)
        start = m.end()
    rest = (text[start:] or "").strip()
    if rest:
        out.append(rest)
    return out


def _fmt_num(v: float) -> str:
    """算式里显示的操作数：整数不带小数点，其余保留两位。"""
    if abs(v - round(v)) < 1e-9:
        return f"{v:,.0f}"
    return f"{v:,.2f}"


def _pool_index(vals: list[float]) -> dict[float, float]:
    """把候选操作数做成「两位小数 → 原值」的索引，供 O(1) 反查。"""
    idx: dict[float, float] = {}
    for v in vals:
        if abs(v) < 1e-9 or abs(v) > 1e9:
            continue
        idx.setdefault(round(v, 2), v)
    return idx


def _lookup(idx: dict[float, float], want: float) -> float | None:
    """在索引里找一个等于 want 的数（容两位小数的舍入误差）。

    容差必须收紧：早先为兼容「材料写 18、正文写 18.00」加了 round(,0) 兜底，
    结果 3.769 被当成 4，凑出 (3.79−4)÷4 这种根本不成立的算式。
    假核对比不核对更糟，所以只按 ±0.02 找，整数兜底要求 want 本身接近整数。
    """
    if not idx or abs(want) > 1e9:
        return None
    k = round(want, 2)
    if k in idx:
        return idx[k]
    for d in (0.01, -0.01, 0.02, -0.02):
        k2 = round(want + d, 2)
        if k2 in idx:
            return idx[k2]
    if abs(want - round(want)) < 0.03:
        ik = float(round(want))
        if ik in idx:
            return idx[ik]
    return None


# ── 按模型交底的算式复算 ─────────────────────────────────────────────────
# 「事后猜算式」只能覆盖同段出现操作数的那一小部分，剩下的大量增速、占比
# 永远猜不出来（正文只写结果、不写分子分母）。所以真正省人工的办法是
# 让模型在 data_notes 里把算式写出来，程序照着复算——
# 算得上的直接销账，算不上的、没交底的才进待核对。
_FORMULA_LINE = re.compile(
    r"^(?P<name>[^=＝]{2,40})[=＝]\s*(?P<expr>[0-9,.\s+\-−×÷*/%()（）]+?)\s*$")


def _parse_formula(note: str) -> dict | None:
    """解析一行 data_notes 算式：『短期债务占比 = 3,421.53 ÷ 5,924.25 × 100%』。

    模型常把结果也写上去（『… × 100% = -1.90%』），这一截在复算时是多余的，
    但正则会被它整行挡住，算式就白交底了——先摘掉末尾的「= 结果」再解析。
    """
    s = (note or "").strip().lstrip("-·•* ").strip()
    m2 = re.match(r"^(?P<head>.+?)[=＝]\s*(?P<val>[-−]?[0-9,]+(?:\.\d+)?)\s*"
                  r"(?P<unit>%|％|个百分点|倍|亿元|万元)?\s*$", s)
    if m2 and re.search(r"[÷×+\-−*/]", m2.group("head").split("=")[-1] if "=" in m2.group("head")
                        else m2.group("head")):
        s = m2.group("head").strip()
    m = _FORMULA_LINE.match(s)
    if not m:
        return None
    name = re.sub(r"^[^·]*·", "", m.group("name")).strip(" ：:")
    # 千分位逗号先去掉：留着会让「3,421.53」被切成 3 和 421.53 两个数
    expr = m.group("expr").replace(",", "").replace("，", "")
    if not re.search(r"[+\-−×÷*/]", expr):
        return None
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", expr)]
    if len(nums) < 2:
        return None
    return {"name": name, "expr": m.group("expr"), "nums": nums, "raw": s}


def _eval_formula(expr: str, nums: list[float]) -> float | None:
    """按四则运算把算式算出来。只认数字与 + − × ÷ * / 和百分号，不执行任意代码。"""
    e = (expr.replace(",", "").replace("，", "")
         .replace("−", "-").replace("－", "-").replace("×", "*").replace("÷", "/")
         .replace(" ", "").replace("%", ""))
    if not re.fullmatch(r"[0-9.+\-*/()]+", e):
        return None
    # 按出现顺序把数字替换成实际值再求值；不能用字符串 replace，
    # 否则「1」会把「100」的前缀吃掉，算式就变了。
    it = iter(nums)
    try:
        filled = re.sub(r"\d+(?:\.\d+)?", lambda m: f"({next(it)!r})", e)
    except StopIteration:
        return None
    if next(it, None) is not None:
        return None
    try:
        out = eval(filled, {"__builtins__": {}}, {})  # noqa: S307 — 已限定字符集
    except Exception:
        return None
    return float(out) if isinstance(out, (int, float)) else None


def _verify_notes(draft: dict, corpus_nums: set[str]) -> tuple[list[dict], list[dict]]:
    """把 data_notes 里交底的算式逐条复算。

    返回 (通过, 不通过)。通过的要求是三条同时成立：
    ① 两个以上操作数；② 每个操作数都能在材料/台账里找到；③ 算出来的值与正文一致。
    比对一律用数值而不是字符串：按字符串取整会把 5.95 和 6.40 当成同一个数。
    """
    ok: list[dict] = []
    bad: list[dict] = []
    c = draft.get("commentary") or {}
    body_text = " ".join([
        draft.get("basic_info") or "",
        ((c.get("industry") or {}).get("body") or ""),
        ((c.get("overview") or {}).get("body") or ""),
        ((c.get("asset") or {}).get("body") or ""),
        ((c.get("profit") or {}).get("body") or ""),
        ((c.get("debt") or {}).get("body") or ""),
        " ".join(s.get("body") or "" for s in (c.get("segments") or [])),
        " ".join(t.get("body") or "" for t in (c.get("special_topics") or [])),
    ])
    body_vals: list[float] = []
    for n in collect_numbers(body_text):
        try:
            body_vals.append(float(re.sub(r"[^\d.]", "", n)))
        except ValueError:
            pass

    for note in (draft.get("data_notes") or []):
        f = _parse_formula(str(note))
        if not f:
            continue
        # ② 操作数是否都在材料里
        missing: list[float] = []
        for v in f["nums"]:
            cands = {f"{v:.2f}", f"{v:.1f}", f"{v:.0f}",
                     (str(int(v)) if v == int(v) else "")}
            if not any(x and x in corpus_nums for x in cands):
                if not any(_close(v, m) for m in missing):
                    missing.append(v)
        val = _eval_formula(f["expr"], f["nums"])
        # ③ 算出来的值是否真的出现在正文里（数值比较，两位小数容差）
        hit_text = val is not None and any(_close(abs(val), b) for b in body_vals)
        row = {"name": f["name"], "expr": f["raw"], "val": val,
               "missing": missing, "hit": hit_text}
        (ok if (val is not None and hit_text and not missing) else bad).append(row)
    return ok, bad



def _calc(mode: str, a: float, b: float) -> float | None:
    """按算式模式实际算一遍。回代校验用，见 _try_formula。"""
    try:
        if mode == "diff":
            return a - b
        if mode == "sum":
            return a + b
        if mode == "ratio":
            return a / b * 100
        if mode == "rest":
            return (a - b) / a * 100
        if mode == "growth":
            return (a - b) / b * 100
        if mode == "times":
            return a / b
    except ZeroDivisionError:
        return None
    return None


_MODES = ("ratio", "rest", "growth", "diff", "sum", "times")


def _formula_table(pool: list[float]) -> dict:
    """把「这一段里任意两个数」能算出的所有结果先算一遍，建成表。

    同段只有一二十个数，两两组合全算出来也就几千条，一次建表反复查，
    比「按 target 反解需要的那个操作数」稳得多：反解要经过除法，
    target 又是两位小数，误差会被放大——42.25% 反解出 3421.24，
    而段里真实的数是 3421.53，差 0.29，查不到。穷举没这个问题。
    """
    vals = sorted({round(v, 4) for v in pool if 1e-9 < abs(v) < 1e9})
    table: dict[tuple[str, float], tuple[float, float, float]] = {}
    for a in vals:
        for b in vals:
            if a == b:
                continue
            for m in _MODES:
                v = _calc(m, a, b)
                if v is None or abs(v) > 1e9:
                    continue
                table.setdefault((m, round(v, 2)), (a, b, v))
    return table


def _try_formula(target: float, kind: str, table: dict,
                 allow: set[str] | None = None) -> tuple[str, str] | None:
    """按 target 去表里找一条成立的算式，返回 (算式文字, 口径名)。

    只在「同一段」的数字里找，不碰材料全文——全文几千个数两两组合，
    几乎能凑出任何 target，那是假核对，比不核对更糟。

    allow 限定可接受的算式类型，用在一处最容易出错的地方：一个裸金额
    （「净利润 -8.39 亿元」）在同一段两两相减几乎总能凑出等值——
    实测它被「13.49 − 5.11 = 8.39」验成了通过，而这两项跟净利润毫无关系。
    所以只有当句子本身在讲差额（增加/减少/上升/下降）时才认差额算式，
    讲合计时才认加总算式；裸金额只能靠材料原文或模型交底的算式。
    """
    if not table:
        return None
    pct = kind in ("占比", "增速", "比率", "比例/增速") or (kind == "" and abs(target) <= 100)
    if kind == "占比":
        orders = ("ratio", "rest")          # 前者 分子÷总数；后者 (总数−分子)÷总数
    elif kind == "增速":
        orders = ("growth",)
    elif kind in ("差额", "金额", "变动幅度"):
        # 「上升 0.63 倍」「增加 5.95 个百分点」都是两个数相减，不是比例
        orders = ("diff", "sum")
    elif kind in ("规模", "结构"):
        orders = ("diff", "sum", "rest")
    elif kind == "倍数/覆盖率":
        # 「同比上升 0.63 倍」是两个倍数相减，不是相除，所以差额优先；
        # 小于 1 的「倍数」只能是差额说法，除法那一档直接不给——
        # 否则 42.25 ÷ 66.97 = 0.63 这种凑巧的算式会被当成核对通过。
        orders = ("diff", "times") if abs(target) >= 1 else ("diff",)
    else:
        orders = ("ratio", "growth", "diff", "sum") if pct else ("diff", "sum")
    if allow is not None:
        orders = tuple(o for o in orders if o in allow)

    for mode in orders:
        for key in (round(target, 2), round(target + 0.01, 2), round(target - 0.01, 2),
                    round(target + 0.02, 2), round(target - 0.02, 2)):
            hit = table.get((mode, key))
            if not hit:
                continue
            a, b, got = hit
            # 操作数不能是 target 自己，否则是自证：0.63 ÷ 1.02 = 0.63
            if _close(a, target) or _close(b, target):
                continue
            if mode in ("ratio", "growth", "times") and (b <= 0 or abs(b - 1) < 1e-9):
                continue          # 除以 1 不是算式
            if not _close(got, target):
                continue
            if mode == "ratio":
                return (f"{_fmt_num(a)} ÷ {_fmt_num(b)} × 100% = {_fmt_num(target)}%", "占比")
            if mode == "rest":
                return (f"({_fmt_num(a)} − {_fmt_num(b)}) ÷ {_fmt_num(a)} × 100% = "
                        f"{_fmt_num(target)}%", "占比")
            if mode == "growth":
                return (f"({_fmt_num(a)} − {_fmt_num(b)}) ÷ {_fmt_num(b)} × 100% = "
                        f"{_fmt_num(target)}%", "增速")
            if mode == "diff":
                unit = " 个百分点" if kind == "变动幅度" else ""
                return (f"{_fmt_num(a)} − {_fmt_num(b)} = {_fmt_num(target)}{unit}", "差额")
            if mode == "sum":
                return (f"{_fmt_num(a)} + {_fmt_num(b)} = {_fmt_num(target)}", "合计")
            if mode == "times":
                return (f"{_fmt_num(a)} ÷ {_fmt_num(b)} = {_fmt_num(target)} 倍", "倍数")
    return None


def _try_formula_corpus(target: float, kind: str, anchors: list[float],
                        corpus_nums: set[str],
                        allow: set[str] | None = None) -> tuple[str, str] | None:
    """用「句中的一个数」配「材料里的另一个数」，反解出这个结果是怎么来的。

    解决的是一类很常见的漏核：「资产减值损失68.17亿元、较上年增加63.06亿元」——
    68.17 在句子里，5.11 只在材料的另一页上，同段穷举永远凑不出来，
    于是这条正确的分析被当成「算不出来」。差的那半个操作数，材料里明明有。

    做法不是穷举（几千个数两两组合能凑出任何值），而是反解：
    差额要 c = a − target，合计要 c = target − a，增速要 c = a ÷ (1 + target)…
    每个锚点只查一个候选值，命中即回代验算。要凑巧命中，得同时满足
    「a 是句子里的数」且「a − target 恰好在材料里」，这不是运气能做到的。
    """
    if not corpus_nums or not anchors:
        return None

    def has(x: float) -> list[float]:
        """反解出来的操作数可能因四舍五入差一点（26.65% 反解出 306.03，
        材料写 306.02），容差放到 0.05；容差内的几个都返回来，由回代裁决。"""
        out: list[float] = []
        for d in (0.0, 0.01, -0.01, 0.02, -0.02, 0.05, -0.05):
            cand = round(x + d, 2)
            if f"{cand:.2f}" in corpus_nums or f"{cand:.1f}" in corpus_nums:
                if not any(_close(cand, o) for o in out):
                    out.append(cand)
        return out

    orders = {"差额": ["diff"], "变动幅度": ["diff"], "金额": ["diff", "sum"],
              "规模": ["diff", "sum"], "结构": ["diff", "sum"],
              "增速": ["growth"], "占比": ["ratio", "rest"],
              "比率": ["ratio", "growth"], "倍数/覆盖率": ["times", "diff"],
              "比例/增速": ["ratio", "growth"]}.get(kind, ["ratio", "growth"])
    if allow is not None:
        orders = [o for o in orders if o in allow]

    unit = " 个百分点" if kind == "变动幅度" else ""
    for a in anchors:
        if a <= 0:
            continue
        for mode in orders:
            if mode == "diff":
                cands = [c for c in has(a - target)
                         # 只挡住「减掉一个近乎为零的数」这种凑数；不按量级配对，
                         # 「收入减少137.69亿元」的两端本来就差着一个量级
                         # （940.42 减 802.73）。
                         if abs(c) >= 0.5 and abs(c) <= max(500 * abs(target), 1)]
            elif mode == "sum":
                cands = has(target - a)
            elif mode == "growth":
                if target <= -100:
                    continue
                cands = has(a / (1 + target / 100))
            elif mode == "ratio":
                cands = has(a * 100 / target) if target else []
            elif mode == "rest":
                cands = has(a * (100 - target) / 100) if target != 100 else []
            else:                       # times
                cands = has(a / target) if target else []
            for c in cands:
                if _close(c, target) or _close(c, a):
                    continue
                got = _calc(mode, a, c)
                # 与调用方给的 target 比（它已经带了符号：降幅传 -26.65），
                # 不能取绝对值——取绝对值就永远比不过算出来的负数。
                if got is None or not _close(got, target):
                    continue
                if mode == "diff":
                    return (f"{_fmt_num(a)} − {_fmt_num(c)} = "
                            f"{_fmt_num(target)}{unit}", "差额")
                if mode == "sum":
                    return (f"{_fmt_num(a)} + {_fmt_num(c)} = {_fmt_num(target)}", "合计")
                if mode == "growth":
                    return (f"({_fmt_num(a)} − {_fmt_num(c)}) ÷ {_fmt_num(c)} × 100% = "
                            f"{_fmt_num(target)}%", "增速")
                if mode == "ratio":
                    return (f"{_fmt_num(a)} ÷ {_fmt_num(c)} × 100% = "
                            f"{_fmt_num(target)}%", "占比")
                if mode == "rest":
                    return (f"({_fmt_num(a)} − {_fmt_num(c)}) ÷ {_fmt_num(a)} × 100% = "
                            f"{_fmt_num(target)}%", "占比")
                return (f"{_fmt_num(a)} ÷ {_fmt_num(c)} = {_fmt_num(target)} 倍", "倍数")
    return None


def _locate_sent(text: str, sent: str, num: str) -> dict:
    """给一个数字找出「在第几句、句内第几个字」，前端据此把那一句选中。"""
    sents = _sentences(text)
    for i, s in enumerate(sents, 1):
        if num in s:
            # 句子里可能有多个相同数字，取第一个还没被占用的
            return {"si": i, "sent": s, "idx": i}
    return {"si": 0, "sent": _cite(sent, num), "idx": 0}


def _index_of_sent(text: str, sent: str) -> int:
    """这一句在整段里是第几句（1 起）。找不到就返回 0，前端只滚到段落。"""
    key = (sent or "").strip()
    if not key:
        return 0
    for i, s in enumerate(_sentences(text), 1):
        if s == key or s.startswith(key[:16]) or key.startswith(s[:16]):
            return i
    return 0



_SUBJ_WORDS = ("公司", "发行人", "集团", "本公司", "其为")

# 以这些词开头，几乎一定是「省略了公司的属性评价句」：
#   「区域专营地位较强」= 公司的区域专营地位较强
#   「股权关系清晰稳定」= 公司的股权关系清晰稳定
# 反过来「水泥熟料产能规模…」「对外担保对象…」条首是具体事物，句子本身完整，
# 不在表里，不报——判不准的宁可漏。
_SUBJ_TOPIC = ("区域", "股权", "治理", "财务", "经营", "规模", "专营", "竞争",
               "外部支持", "融资", "资本", "盈利", "偿债", "流动", "信用",
               "资产质量", "债务负担", "抗风险")


def _subj_lead(lead: str) -> bool:
    """加粗汇总句里有没有出现受评主体。

    只查「有没有」，不判句法对错：主语可以是公司、股东、政府、行业，
    也可以承接上一句省略——机器判不准哪种省略合格。但一句判断从头到尾
    没出现过主体，读者就只能猜，这是能确定的不合格。

    放宽一个口子：整句主语是外部条件（行业、政策、价格、需求）时，
    不出现公司并不算错——「水泥价格中枢下移压制毛利」主语就是价格。
    所以这类开头直接放过，不报。
    """
    s = (lead or "").strip()
    if not s:
        return True                     # 没写加粗句是另一条规则的事
    if any(w in s for w in _SUBJ_WORDS):
        return True
    return any(s.startswith(w) for w in (
        "行业", "政策", "区域需求", "市场需求", "价格", "水泥", "钢材", "煤炭",
        "利率", "汇率", "下游", "上游", "竞争"))


def _subj_ratio(items: list[str]) -> tuple[int, int]:
    """统计有多少条是「拿属性当主语」的省略句，返回 (命中的, 总数)。

    原来的判法是「条首是不是公司／发行人／集团」，那是个错的标准：
    「水泥熟料产能规模居行业前列」「新材料板块收入占比持续提升」
    「对外担保对象集中于区属国企」都是完整句，主语就是那个名词短语，
    照原来的判法会被一起算成缺主语——四条里三条合格，却报「只有 1/4 条有主语」。
    提示一旦系统性误报，人就再也不看了。

    真正缺主语的是另一类：条首是**评价性属性词**，把它当成了主语在评价，
    例如「区域专营地位较强」「股权关系清晰稳定」「财务弹性充足」——
    这些句子的意思是「公司这些方面……」，省略的正是公司。
    所以只统计这类条首；判不准的一律放过，宁可漏报。
    """
    rows = [x for x in (items or []) if (x or "").strip()]
    hit = sum(1 for x in rows if x.strip().startswith(_SUBJ_TOPIC))
    return hit, len(rows)


# 「没有变化的事」的识别式。每一条都是「这件事没变」的例行交代，
# 或者与偿债能力无关的人事变动——它们出现在跟踪评级报告里是因为栏目固定，
# 但对读者判断「能不能还钱」没有信息量。
#
# 刻意不做的两件事：
# ① 不去判「变化重不重要」。机器判不了。所以这里只认「明确写了没变」的句式，
#    以及人事任免这一个公认与偿债无关的类别，其余一律放过。
# ② 不把「评级下调」「列入观察名单」算进来——那是变化，而且要写。
_NOCHANGE_PATTERNS = (
    # 股权、股东、持股比例没变
    ("股东/持股未变",
     r"(?:控股股东|股东|股权结构|股权关系|持股比例|实际控制人|控制权)"
     r"[^。；]{0,24}?(?:未发生|没有发生|未|没有)(?:发生)?(?:变化|变动|变更|调整)"),
    # 评级维持原级、展望稳定。两个方向都要认：「等级维持AA+」与「维持等级AA+」，
    # 只写一种顺序会漏掉另一种——评级报告里「联合资信维持公司主体信用等级AA+」
    # 是最常见的那一句。
    ("评级未变",
     r"(?:信用等级|主体信用等级|评级|级别)[^。；]{0,20}?(?:维持|保持|仍为|未变|未调整|不变)"),
    ("评级未变",
     r"(?:维持|保持)[^。；]{0,14}?(?:信用等级|主体信用等级|主体评级|评级|级别)"),
    # 审计机构没换
    ("审计机构未换",
     r"(?:审计机构|会计师事务所|审计单位)[^。；]{0,16}?(?:未|没有)(?:发生)?(?:变更|更换|变化)"),
    # 董监高人事变动
    ("董监高变更",
     r"(?:董事长|总经理|总裁|副总经理|财务负责人|董事会秘书|监事会主席|董事|监事|高级管理人员)"
     r"[^。；]{0,20}?(?:由[^。；]{1,16}?变更|变更为|改由|更换为|不再担任|辞任|离任|新任)"),
)


def _nochange_rows(draft: dict) -> list[dict]:
    """找出「没有变化的事」被写进正文的地方。

    只扫基本情况与各段正文，不扫摘要——摘要本来就是定性概括，
    真写了「评级未变」会在摘要层的其他规则里被抓到。

    「评级维持」这条要留一个口子：句子里同时出现下调／上调／观察／负面，
    说明写的是变化，不报。否则「评级由 AA+ 上调至 AAA，展望维持稳定」
    会被前半句「维持」误伤。
    """
    c = draft.get("commentary") or {}
    fields: list[tuple[str, str, str]] = [
        ("基本情况", "basic", "blk-basic", draft.get("basic_info") or ""),
        ("行业情况", "in-body", "blk-industry", (c.get("industry") or {}).get("body") or ""),
        ("总述段", "ov-body", "blk-overview", (c.get("overview") or {}).get("body") or ""),
        ("资产段", "as-body", "blk-asset", (c.get("asset") or {}).get("body") or ""),
        ("盈利段", "pf-body", "blk-profit", (c.get("profit") or {}).get("body") or ""),
        ("债务段", "db-body", "blk-debt", (c.get("debt") or {}).get("body") or ""),
    ]
    rows: list[dict] = []
    seen: set[str] = set()
    for where, field, anchor, text in fields:
        for kind, pat in _NOCHANGE_PATTERNS:
            for m in re.finditer(pat, text or ""):
                # 取这一句，作为定位与判断的完整单位
                cut = max(text.rfind(ch, 0, m.start()) for ch in "。；") + 1
                ends = [x for x in (text.find(ch, m.start()) for ch in "。；") if x > 0]
                sent = text[cut: min(ends) if ends else len(text)].strip()
                if not sent or sent in seen:
                    continue
                if kind == "评级未变" and re.search(r"下调|上调|列入观察|观察名单|负面|撤销", sent):
                    continue          # 写的是评级变化，不是「没变」
                seen.add(sent)
                rows.append({"num": "", "where": f"{where}·{kind}", "kind": "无变化",
                             "full": sent[:180], "si": 0,
                             "anchor": anchor, "field": field,
                             "note": "不影响偿债判断的变化/未变，整句删掉"})
    return rows


# 校验规则的版本号。规则一改就加一，存量记录据此判断要不要重算——
# 只看「有没有 issue_items」是不够的：新加的校验项不会进到老记录里，
# 用户打开旧报告就看不到新提示，等于没上。
VALIDATOR_REV = 8

# 审计意见的判定表，按严重程度从高到低。只认这三种非标准意见与「持续经营重大
# 不确定性」段落：标准无保留意见不必写（19.2），而「强调事项」在部分事务所的
# 模板里是常设栏目，拿来当漏项会大批误报，所以不进校验，只在提示词里提一句。
_AUDIT_OPINIONS = (
    ("无法表示意见", ("无法表示意见",), ("无法表示意见",)),
    ("否定意见", ("否定意见",), ("否定意见",)),
    ("保留意见", ("保留意见",), ("保留意见",)),
    ("与持续经营相关的重大不确定性",
     ("持续经营相关的重大不确定性", "持续经营重大不确定性"), ("持续经营",)),
)


def _audit_opinion(materials: list[dict]) -> dict | None:
    """从审计报告材料里认出非标准审计意见；标准无保留意见返回 None。

    关键坑：「无保留意见」里含「保留意见」四个字，标准报告必然出现，直接搜
    「保留意见」会把每一份干净的报告都当成保留意见。所以先把「无保留意见」
    整体遮掉，再判保留意见。
    """
    for mat in materials or []:
        kind = mat.get("kind") or ""
        title = mat.get("title") or ""
        if kind != "审计报告" and "审计" not in title:
            continue
        text = mat.get("text") or ""
        if not text:
            continue
        masked = text.replace("无保留意见", "标准意见")
        for phrase, triggers, marks in _AUDIT_OPINIONS:
            hit = next((t for t in triggers if t in masked), None)
            if not hit:
                continue
            i = masked.find(hit)
            sent = text[max(0, i - 60):i + 90].replace("\n", " ").strip()
            return {"phrase": phrase, "marks": list(marks), "sentence": sent,
                    "title": title}
    return None


def validate(draft: dict, facts: dict, materials: list[dict]) -> dict:
    """数字溯源校验 + 摘要层数据扫描 + 结构检查。

    返回 issues（人读的一句话）与 issue_items（结构化，带段/句定位，前端据此逐条跳转）。
    auto_checks 是机器已经自己算过、不需要人工看的那部分，只作留痕。
    """
    issues: list[str] = []
    issue_items: list[dict] = []
    auto_notes: list[str] = []
    auto_ok: list[dict] = []

    # 全文拼一份，供「正文到底写了没有」这类判断用（与后文 labeled 同源，
    # 但那些块在几百行之后才拼，早点的检查先用这份）
    _c0 = draft.get("commentary") or {}
    _chunks = [draft.get("basic_info") or ""]
    for _k in ("industry", "overview", "asset", "profit", "debt"):
        _b = _c0.get(_k) or {}
        _chunks += [_b.get("lead") or "", _b.get("body") or ""]
    for _b in (_c0.get("segments") or []):
        _chunks.append(_b.get("body") or "")
    for _b in (_c0.get("special_topics") or []):
        _chunks += [_b.get("lead") or "", _b.get("body") or ""]
    _body_all = "\n".join(_chunks)

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

    # 1.1) 摘要层措辞：只分等级，不做评价。
    # 摘要是最先被读的部分，也最容易写成「优势突出」「弹性充足」这类空话——
    # 它们听起来是判断，实际没有刻度，读者读不出强到什么程度，也无法跨主体比较。
    hype_rows: list[dict] = []
    long_rows: list[dict] = []
    for key, label, anchor, field in (("positive", "正面", "blk-pos", "pos"),
                                      ("concern", "关注", "blk-con", "con")):
        for i, item in enumerate(draft.get(key) or [], 1):
            t = (item or "").strip()
            if not t:
                continue
            words = [w for w in _HYPE_WORDS if w in t]
            words += [m.group(0) for m in _HYPE_PAT.finditer(t) if m.group(0) not in words]
            # 「表现较好」里已经含「较好」，只留更具体的那个，同一个词不报两遍
            words = [w for w in dict.fromkeys(words)
                     if not any(w != o and w in o for o in words)]
            if words:
                hype_rows.append({
                    "num": "、".join(words), "where": f"{label}{i}）",
                    "kind": "措辞", "full": t, "si": 0, "seg": i,
                    "anchor": anchor, "field": field,
                    "note": "换成有刻度的分级词：很强／较强／一般／较弱、"
                            "充足／尚可／承压／不足、偏高／合理、集中／分散",
                })
            if len(t) > 55 or "；" in t or ";" in t:
                long_rows.append({
                    "num": f"{len(t)} 字", "where": f"{label}{i}）", "kind": "篇幅",
                    "full": t, "si": 0, "seg": i, "anchor": anchor, "field": field,
                    "note": f"{len(t)} 字" + ("，含分号并列" if ("；" in t or ";" in t) else "")
                            + "；一条只讲一件事，拆成两条或删掉并列的那半句",
                })

    if hype_rows:
        sample = "；".join(f"{r['where']}「{r['num']}」" for r in hype_rows[:4])
        issues.append(
            f"摘要层有 {len(hype_rows)} 条用了没有刻度的评价词（{sample}）。"
            "摘要只分等级不做评价：能力、地位、支持力度用很强／较强／一般／较弱，"
            "流动性、覆盖用充足／尚可／承压／不足，占比、杠杆用偏高／合理。"
            "「显著」「突出」「良好」「充裕」这类词换个人写就落在另一档，读者无法据此比较主体。")
        issue_items.append({"t": "摘要层措辞没有刻度", "a": "blk-pos", "d": hype_rows[:8]})

    # 1.15) 主语：优先靠提示词（规则 3.1／21.3）在生成时就写对，
    # 这里只是漏网时的兜底。所以不做逐条清单——列出八条让人一条条改，
    # 比不提示还累；只说明「哪一段的加粗句看不出主体」和覆盖情况。
    _c0 = draft.get("commentary") or {}
    _leads = [("行业段", "in-lead", "blk-industry", (_c0.get("industry") or {}).get("lead")),
              ("总述段", "ov-lead", "blk-overview", (_c0.get("overview") or {}).get("lead")),
              ("资产段", "as-lead", "blk-asset", (_c0.get("asset") or {}).get("lead")),
              ("盈利段", "pf-lead", "blk-profit", (_c0.get("profit") or {}).get("lead")),
              ("债务段", "db-lead", "blk-debt", (_c0.get("debt") or {}).get("lead"))]
    _no_subj = [(name, field, anchor, (lead or "").strip())
                for name, field, anchor, lead in _leads if not _subj_lead(lead)]
    if _no_subj:
        _names = "、".join(n for n, *_ in _no_subj)
        issues.append(
            f"{_names}的加粗汇总句里没有出现受评主体，读起来不知道在说谁。判断句要有主语："
            "「公司为……」「公司作为……」，或明确换成行业、政策这类外部主语。")
        issue_items.append({
            "t": "加粗句缺主语", "a": _no_subj[0][2],
            "d": [{"num": "", "where": f"{n}加粗句", "kind": "主语",
                   "full": s[:160], "si": 0, "anchor": a, "field": f,
                   "note": "补主语：公司为……／公司作为……；主语是外部条件时改成明写行业、政策"}
                  for n, f, a, s in _no_subj]})

    for key, label in (("positive", "正面"), ("concern", "关注")):
        # hit 是「以属性词开头的条数」，也就是疑似省略了公司的那几条。
        # 多了才提示：过半就说明这一类写法已经成了习惯，不是个别。
        hit, tot = _subj_ratio(draft.get(key) or [])
        if tot >= 3 and hit * 2 > tot:
            issues.append(f"{label}里有 {hit}/{tot} 条拿属性当主语（「区域专营地位较强」"
                          "「股权关系清晰稳定」这类），省略的正是公司。"
                          "摘要从正文抽句时容易连主语一起删掉，抽完回头读一遍："
                          "这一条单独拿出来，读的人知道说的是谁吗。")
            issue_items.append({
                "t": f"{label}条目主语", "a": "blk-pos" if key == "positive" else "blk-con",
                "d": [{"num": "", "where": f"{label}（共 {tot} 条，{hit} 条以属性词开头）",
                       "kind": "主语", "full": "；".join(
                           (x or "")[:40] for x in (draft.get(key) or [])[:3]),
                       "si": 0, "seg": 0,
                       "anchor": "blk-pos" if key == "positive" else "blk-con",
                       "field": "pos" if key == "positive" else "con",
                       "note": "改成「公司为……」或「公司作为……」开头"}]})

    if long_rows:
        issues.append(
            "摘要条目偏长或并列（" + "、".join(f"{r['where']}{r['note'].split('；')[0]}"
                                          for r in long_rows[:4])
            + "）：一条只讲一件事、一句话，把并列的那半句拆出去。")
        issue_items.append({"t": "摘要一条塞了两件事", "a": "blk-pos", "d": long_rows[:8]})

    # 1.16) 行业情况：必须是独立一段，且每句站得住
    # 这一段此前不存在，行业的量价被揉进总述段，与公司自己的收入结构混在一起，
    # 读的人分不清哪句说行业、哪句说公司。规则 12.1 要求单列，这里兜底检查。
    _ind = _c0.get("industry") or {}
    _ind_body = str(_ind.get("body") or "").strip()
    if not (_ind_body or str(_ind.get("lead") or "").strip()):
        issues.append(
            "缺少「行业情况」一段。行业处在什么阶段、量由什么决定、价由谁定，"
            "这一段要单独写，不能揉进总述段与公司自己的收入结构混在一起。")
        issue_items.append({"t": "缺行业情况段", "a": "blk-industry",
                            "d": [{"num": "", "where": "行业情况", "kind": "结构",
                                   "full": "", "si": 0, "anchor": "blk-industry",
                                   "field": "in-body",
                                   "note": "补一段：供需 → 价格与成本 → 政策 → 对公司的传导"}]})
    elif len(_ind_body) < 120:
        issues.append(f"行业情况只有 {len(_ind_body)} 字，"
                      "写不完整就退化成一句行业概述。按供需 → 价格与成本 → 政策 → 传导四件事展开。")
        issue_items.append({"t": "行业情况偏薄", "a": "blk-industry",
                            "d": [{"num": f"{len(_ind_body)} 字", "where": "行业情况",
                                   "kind": "写法", "full": _ind_body[:140], "si": 0,
                                   "anchor": "blk-industry", "field": "in-body",
                                   "note": "补价格与政策两条，并写清对公司的传导"}]})
    elif not re.search(r"\d", _ind_body):
        issues.append(
            "行业情况整段没有一个数字。行业分析靠的是产量增速、产能利用率、价格涨跌幅、"
            "区域投资额这类可核实的量，纯定性的行业判断换个人写也一样，读者据此比不了强弱。")
        issue_items.append({"t": "行业段没有数据", "a": "blk-industry",
                            "d": [{"num": "缺数据", "where": "行业情况", "kind": "写法",
                                   "full": _ind_body[:140], "si": 0,
                                   "anchor": "blk-industry", "field": "in-body",
                                   "note": "每个判断后面跟一个数；材料只给定性说法时，写到公司自己的量价上"}]})

    # 1.17) 基本情况：注册资本与实收资本
    # 这两项是判断「股东到底投了多少钱」的唯一依据，注册了不等于实缴了。
    # 只在材料里确实出现「实收资本」时要求写——材料没给的不能编（规则 5）。
    _basic = str(draft.get("basic_info") or "")
    if _basic.strip():
        _mat_text = "\n".join((m.get("text") or "") for m in materials)
        _has_reg = "注册资本" in _basic
        _has_paid = "实收资本" in _basic
        _mat_has_paid = "实收资本" in _mat_text
        if not _has_reg or (_mat_has_paid and not _has_paid):
            _miss = []
            if not _has_reg:
                _miss.append("注册资本")
            if _mat_has_paid and not _has_paid:
                _miss.append("实收资本")
            issues.append(
                f"基本情况没有写「{'」「'.join(_miss)}」。注册资本是认缴额、实收资本是到账额，"
                "两项差着就说明还有没缴的部分，这直接影响股东支持的成色。"
                + ("材料里有实收资本，请一并写出；两者不一致时说明差额是什么。"
                   if _mat_has_paid else "请补注册资本；材料给出实收资本时也一并写。"))
            issue_items.append({"t": "基本情况缺注册资本/实收资本", "a": "blk-basic",
                                "d": [{"num": "、".join(_miss), "where": "基本情况",
                                       "kind": "结构", "full": _basic[:140], "si": 0,
                                       "anchor": "blk-basic", "field": "basic",
                                       "note": "写清注册资本与实收资本；两者不一致说明差额原因"}]})

    # 1.18) 没有变化的事不写
    # 跟踪报告里最占字数的往往不是变化，而是「未发生变化」的例行交代：
    # 控股股东没变、评级没调、董事长换人。这些不影响偿债判断，却挤掉真正的分析。
    _nochange = _nochange_rows(draft)
    if _nochange:
        _kinds = "、".join(dict.fromkeys(r["kind"] for r in _nochange))
        issues.append(
            f"正文写了 {len(_nochange)} 处没有变化的事（{_kinds}）。"
            "报告只记录变化和它的影响：股东与持股比例没变、评级维持原级、审计机构未换，都不写；"
            "董事长、总经理这类人事更替与偿债能力无关，也不写。"
            "判断方法——把这件事写成一条公告，读者会不会改变「能不能按期还钱」的判断？不会就删掉。")
        issue_items.append({"t": "写了没有变化的事", "a": "blk-basic", "d": _nochange[:6]})

    # 1.1b) 审计意见非标准、正文却没交代——这是读过审计报告之后最要紧的一条。
    # 判据只看审计报告材料里的定性结论，不去猜：出现「保留意见」「无法表示意见」
    # 「否定意见」或「与持续经营相关的重大不确定性」，而正文一个字没提，就是漏了。
    # 注意「无保留意见」里含「保留意见」子串，必须先把它遮掉再判断。
    _opin = _audit_opinion(materials)
    if _opin and not any(k in _body_all for k in _opin["marks"]):
        issues.append(
            f"审计报告是「{_opin['phrase']}」，正文没有交代。"
            "非标准意见本身就是偿债判断的一部分：写清是哪一类意见、出具的事由（涉及哪笔债务、"
            "哪项资产或哪个金额），放在基本情况或债务段；标准无保留意见不必写。")
        issue_items.append({"t": "审计意见非标准，正文没写", "a": "blk-basic",
                            "d": [{"num": "", "where": "基本情况/债务段",
                                   "full": _opin["sentence"][:120], "si": 0,
                                   "kind": "漏项", "anchor": "blk-basic",
                                   "field": "bi-body", "seg": 0,
                                   "note": f"审计报告：{_opin['phrase']}"}]})

    # 1.2) 同一事实在正面与关注各说一次，等于自相矛盾
    pos_items = [x for x in (draft.get("positive") or []) if (x or "").strip()]
    con_items = [x for x in (draft.get("concern") or []) if (x or "").strip()]
    dup_sum: list[dict] = []
    for i, p in enumerate(pos_items, 1):
        for j, q in enumerate(con_items, 1):
            if is_real_dup(_core_text(p), _core_text(q), 12, min_han=8):
                dup_sum.append({"num": "", "where": f"正面{i}）与关注{j}）", "kind": "重复",
                                "full": p, "si": 0, "seg": i,
                                "anchor": "blk-pos", "field": "pos",
                                "note": f"与关注{j}）说的是同一件事：{q[:40]}"})
                break
    if dup_sum:
        issues.append("正面与关注重复了同一事实（" + "、".join(r["where"] for r in dup_sum[:3])
                      + "），一条事实只能出现在一侧：留在更能说明偿债含义的那一侧。")
        issue_items.append({"t": "正面与关注说了同一件事", "a": "blk-pos", "d": dup_sum[:6]})

    # 1.3) 资产段：科目金额必须带占比，科目要落到「主要为……」
    # 这一块最容易滑回「应收账款规模较大，回收存在一定不确定性」的旧写法：
    # 没有金额与占比，读者判断不了这个科目压了多少资产；没有「主要为」，
    # 也判断不了它装的是工程结算款还是往来款。规则 8 写归写，仍要提示。
    #
    # 只查每句（。；切分）里的第一个金额：那是这句话的主语科目。句中的金额
    # 多是明细、减值准备、投资收益（「贡献投资收益 13.49 亿元」「计提减值
    # 准备 183.09 亿元」），要求它们各自带占总资产比重是错的要求——
    # 实测一份写得完全合格的资产段会被这条规则报出十条，提示就失效了。
    _c = draft.get("commentary") or {}
    as_body = ((_c.get("asset") or {}).get("body") or "")
    if as_body:
        bare: list[tuple[str, str]] = []
        clauses = [x.strip() for x in re.split(r"[。；;]", as_body) if x.strip()]
        for seg in clauses:
            m = re.search(r"[\d,]+\.\d{2}\s*亿元", seg)
            if not m:
                continue
            # 「资产总额 1,000.00亿元」是分母，不是科目，不要求它自己带占比
            if re.search(r"(?:资产总额|总资产|资产规模)[^，]{0,6}$", seg[:m.start()]):
                continue
            if "%" in seg[m.end():] or "％" in seg[m.end():]:
                continue
            bare.append((m.group(0), seg))
        no_main = "主要为" not in as_body and "主要系" not in as_body
        if len(bare) >= 2:
            rows: list[dict] = [{
                "num": re.sub(r"\s", "", amt), "where": "资产段",
                "kind": "写法", "full": seg[:140], "si": 0,
                "anchor": "blk-asset", "field": "as-body",
                "note": "金额后面补「（占资产总额 ××.××%）」，再交代「主要为……」与质量",
            } for amt, seg in bare[:5]]
            if no_main:
                rows.append({
                    "num": "缺「主要为」", "where": "资产段", "kind": "写法",
                    "full": as_body[:120], "si": 0,
                    "anchor": "blk-asset", "field": "as-body",
                    "note": "每个科目都要说清装的是什么（对手方、用途、形态）",
                })
            issues.append(
                f"资产段有 {len(bare)} 个科目只给了金额没给占比"
                + ("，整段也没有「主要为……」" if no_main else "")
                + "。每个科目按「科目 金额（占资产总额 ××.××%），主要为×××，"
                  "再交代能否变现／是否质押／账龄」写，读者才知道压了多少资产、是什么。")
            issue_items.append({"t": "资产段缺金额占比", "a": "blk-asset", "d": rows[:6]})

    # 1.4) 或有事项：材料里给了担保、诉讼的实数，正文一处不提，也没给补段建议。
    # 或有事项不进债务段（规则 9），但也不能因此消失——它能单列就提示用户补一段。
    _whole = " ".join(
        [str(draft.get("basic_info") or "")]
        + [str((_c.get(k) or {}).get("body") or "") for k in ("industry", "overview", "asset", "profit", "debt")]
        + [str(s.get("body") or "") for s in (_c.get("segments") or [])]
        + [str(t.get("body") or "") for t in (_c.get("special_topics") or [])])
    _mat = "\n".join((m.get("text") or "") for m in materials)
    _hints = [w for w in ("对外担保", "担保余额", "未决诉讼", "重大诉讼", "仲裁事项") if w in _mat]
    if _hints and not re.search(r"担保|诉讼|仲裁", _whole) and not (draft.get("add_on_sections") or []):
        issues.append(
            f"材料里出现了或有事项线索（{'、'.join(_hints[:3])}），但正文与专题段都没有相应内容，"
            "也没有给出补段建议。或有事项不写进债务段，可考虑在债务段之后单列一段："
            "担保余额、占净资产比重、被担保方是谁、是否已出现代偿；诉讼则写涉案金额与进展。")
        issue_items.append({"t": "或有事项没落到段落", "a": "blk-topics", "d": []})

    # 1.5) 盈利段：收入与利润都要写，亏损要归因，归因要带数。
    # 这一块的病不是「漏了数据」，是「把要解释的事换了个说法」：
    # 利润从 96 亿掉到 12 亿，正文只写「盈利承压，受行业景气度影响」——
    # 读者看完不知道钱是被谁吃掉的，也无法判断下一期还吃不吃。
    _pf_body = str((_c.get("profit") or {}).get("body") or "").strip()
    _fin = _c.get("financial_table") or {}

    def _num_of(v: str) -> float | None:
        t = re.sub(r"[^\d.\-]", "", str(v).replace("−", "-").replace("－", "-"))
        if t in ("", "-", ".", "-."):
            return None
        try:
            return float(t)
        except ValueError:
            return None

    # 表里的利润行：表格由程序按台账生成，比正文可靠，先用它判断亏损与降幅
    _profit_rows: dict[str, list[float | None]] = {}
    for r in (_fin.get("rows") or []):
        cells = [str(x) for x in (r or [])]
        if cells and re.search(r"净利润|利润总额|归母净利润", cells[0]):
            _profit_rows[cells[0]] = [_num_of(v) for v in cells[1:]]
    _loss_from = ""          # 亏损的判据：说明是表还是材料
    _loss_num = ""
    _loss_nm = ""
    for nm, vals in _profit_rows.items():
        nums = [v for v in vals if v is not None]
        if nums and min(nums) < 0:
            _loss_from, _loss_nm, _loss_num = "财务表", nm, f"{min(nums):,.2f}"
            break
    # 表里没有负值不等于没亏：模型整理财务表时常把「净利润」换成「利润总额」，
    # 亏损就藏在材料的一句话里（「计提减值，导致净利润出现亏损」）。
    _loss_hint = ""
    if not _loss_from:
        mm = re.search(r"净利润[^。；]{0,24}?(?:亏损|为负|-[\d,]+\.\d{2})"
                       r"|(?:亏损|净亏损)[^。；]{0,12}?[\d,]+\.\d{2}\s*亿元", _mat)
        if mm:
            _loss_hint = mm.group(0)[:60]
            _loss_from = "材料"
    _drop = None
    for nm, vals in _profit_rows.items():
        nums = [v for v in vals if v is not None]
        if len(nums) >= 2 and nums[1]:
            d = (nums[0] - nums[1]) / abs(nums[1]) * 100
            if d < -20:
                _drop = (nm, round(d, 2), nums[0], nums[1])
                break
    _all_body = " ".join(
        [str((_c.get(k) or {}).get("body") or "") for k in ("overview", "asset", "profit", "debt")]
        + [str(s.get("body") or "") for s in (_c.get("segments") or [])])
    _has_profit_word = bool(re.search(r"净利润|利润总额|净亏损", _all_body))
    # 「有没有说亏损」只看盈利段：亏损若只在债务段里顺带提一句（「受利润亏损影响，
    # 所有者权益下降」），那是拿它解释别的事，不算对亏损作了交代。
    _says_loss = bool(re.search(r"亏损|净亏损|由盈转亏", _pf_body))
    _loss_elsewhere = bool(re.search(r"亏损|净亏损", _all_body)) and not _says_loss

    # ① 盈利段空着：位置空着，利润就会被塞进债务段或资产段里当一句附带
    if not _pf_body and _profit_rows:
        issues.append(
            "点评里没有盈利段（财务指标表之后、债务段之前这一段空着）。"
            "收入与利润的分析要单独成段：收入拆量、价、结构，利润写总额与净额及同比，"
            "亏损或大幅下滑再逐项归因。")
        issue_items.append({"t": "缺盈利段", "a": "blk-profit", "d": []})

    # ② 通篇不提利润：只写了收入不写利润
    if not _has_profit_word and (_pf_body or _all_body.strip()):
        issues.append(
            "正文只交代了收入，没有利润总额与净利润——收入涨跌不等于赚钱能力变化，"
            "读者看不出这一年到底赚没赚钱。请在盈利段补上利润总额与净利润的本期数与同比。")
        issue_items.append({"t": "只写收入没写利润", "a": "blk-profit",
                            "d": [{"num": "", "where": "盈利段", "kind": "写法",
                                   "full": _pf_body[:120] or "（盈利段为空）", "si": 0,
                                   "anchor": "blk-profit", "field": "pf-body",
                                   "note": "补利润总额与净利润的本期数与同比"}]})

    # ③ 已经亏了，盈利段却不肯写「亏损」二字
    if _loss_from and _pf_body and not _says_loss:
        where = (f"财务表里 {_loss_nm} 为 {_loss_num} 亿元" if _loss_from == "财务表"
                 else f"材料里写着「{_loss_hint}」")
        extra = ("正文别处只有一句顺带提到亏损，还是拿它去解释所有者权益下降的，"
                 "读者仍然不知道亏在哪。" if _loss_elsewhere else "")
        issues.append(
            f"{where}，盈利段没有任何一处写「亏损」，只用了「承压」「下滑」这类说法。"
            + extra +
            "亏损是利润表上的事实，不是程度问题——请直接写明哪一年亏了多少，再讲亏在哪。")
        issue_items.append({"t": "亏损没写成亏损", "a": "blk-profit",
                            "d": [{"num": (_loss_nm + " " + _loss_num).strip() or "亏损",
                                   "where": "盈利段", "kind": "写法",
                                   "full": _pf_body[:140], "si": 0,
                                   "anchor": "blk-profit", "field": "pf-body",
                                   "note": "明写「亏损」与金额，再逐项归因"}]})

    # ④ 亏损或大幅下滑，却没有带数的归因（盈利段都没写时由 ① 负责，不重复报）
    if _pf_body and (_loss_from or _drop):
        # 归因载体：这些词出现、且同一句里有金额／幅度，才算真的归了因
        carriers = ("减值", "信用减值", "毛利率", "毛利", "费用", "投资收益",
                    "公允价值", "补助", "其他收益", "处置", "所得税",
                    "售价", "均价", "销量", "价格")
        strong: list[str] = []
        empty: list[str] = []
        for sent in re.split(r"[。；]", _pf_body or _all_body):
            s = sent.strip()
            if not s or not any(w in s for w in carriers):
                continue
            if re.search(r"\d+(?:\.\d+)?\s*(?:亿元|万元|%|％|个百分点|倍)", s):
                strong.append(s)
            else:
                empty.append(s)
        # 只挂大环境的原因：既不带数，也没说清是哪一项
        vague = [v for v in re.findall(
            r"(?:受|由于|主要系|主要是因为)([^，。；]{2,18}?)(?:影响|所致|拖累)", _pf_body or "")
            if re.search(r"行业|宏观|市场|景气|环境|竞争|周期|政策|需求疲弱", v)]
        enough = 2 if _loss_from else 1
        if len(strong) < enough:
            why = (f"{_loss_from}显示已亏损"
                   + (f"（{_loss_nm} {_loss_num} 亿元）" if _loss_from == "财务表"
                      else f"（{_loss_hint}）")
                   if _loss_from else
                   f"财务表显示{_drop[0]}由 {_drop[3]:,.2f} 降到 {_drop[2]:,.2f} 亿元、"
                   f"降幅 {abs(_drop[1])}%")
            issues.append(
                f"{why}，但盈利段里只找到 {len(strong)} 句带金额的归因"
                + (f"，其余只写了「{empty[0][:32]}…」" if empty else "")
                + "。亏损要一项一句讲清：毛利变动（量、价、成本各多少）→ 期间费用 → "
                  "减值损失（资产减值与信用减值分别是多少、提在哪项资产上）→ 投资收益、"
                  "政府补助、资产处置、所得税，每项带金额，并按影响大小排序。"
                  "写「受行业景气度影响」这类不带数的话，等于把要解释的事换了个说法。")
            issue_items.append({
                "t": "亏损原因没说清", "a": "blk-profit",
                "d": [{"num": "缺金额归因", "where": "盈利段", "kind": "写法",
                       "full": s[:160], "si": 0,
                       "anchor": "blk-profit", "field": "pf-body",
                       "note": "补金额与影响方向"}
                      for s in (empty or [_pf_body or _all_body[:160]])[:4]]})
        # ⑤ 有金额归因，但空话仍占着原因的位置
        elif vague and len(vague) >= len(strong):
            issues.append(
                f"盈利段的归因仍以「受{vague[0]}影响」这类说法为主（{len(vague)} 处），"
                f"带金额的只有 {len(strong)} 句。空话可以留作背景，但不能顶在原因的位置上——"
                "读者要的是「哪一项、多少钱」，不是「大环境不好」。")
            issue_items.append({"t": "归因还被空话占着", "a": "blk-profit",
                                "d": [{"num": "", "where": "盈利段", "kind": "写法",
                                       "full": vague[0], "si": 0,
                                       "anchor": "blk-profit", "field": "pf-body",
                                       "note": "把这一处换成具体项与金额"}]})
    # ⑥ 财务表缺「净利润」行：只列利润总额时，亏没亏在表上根本看不出来
    _fin_names = [str((r or [""])[0]) for r in (_fin.get("rows") or [])]
    if _fin_names and not any(re.search(r"净利润", nm) for nm in _fin_names):
        has_np = bool(re.search(r"净利润", _mat))
        if has_np:
            issues.append(
                "财务指标表里没有「净利润」行"
                + ("（表里有「利润总额」）" if any("利润总额" in nm for nm in _fin_names) else "")
                + "。利润总额扣掉所得税才是净利润，亏损与否看的是净利润；"
                  "缺这一行，正文说「亏损」在表上找不到依托，说「盈利」也看不出薄厚。"
                  "材料里有净利润的，请补一行。")
            issue_items.append({"t": "财务表缺净利润行", "a": "blk-fintable",
                                "d": [{"num": "缺净利润", "where": "财务指标表",
                                       "kind": "结构", "full": "、".join(_fin_names[:8]),
                                       "si": 0, "anchor": "blk-fintable", "field": "table",
                                       "note": "补「净利润」行，与营业总收入同列"}]})
    elif _says_loss:
        # 有行但整行都是「—」，而正文讲了亏损：读者会问到底亏多少。
        # 材料只定性说「净利润出现亏损」时，不许编金额——但要把量级交代清楚，
        # 用能核的数（利润总额、减值金额）把亏损的规模撑起来。
        np_row = next((r for r in (_fin.get("rows") or [])
                       if str((r or [""])[0]).strip() and "净利润" in str(r[0])), None)
        if np_row and all(_num_of(v) is None for v in list(np_row)[1:]):
            # 表里空着、正文却给了金额，必有一处不对：金额若来自材料，就该填进表；
            # 若来路不明，就是编的——两种都得停下来看一眼。
            num_in_body = re.search(r"净利润[^，。；]{0,6}?(-?[\d,]+\.\d{2})\s*亿元",
                                    _all_body)
            if num_in_body:
                issues.append(
                    f"正文写了「净利润 {num_in_body.group(1)} 亿元」，财务表里这一行却是「—」。"
                    "两者必须一致：金额有材料支撑就填进表，没有就把正文改成定性表述"
                    "（写明亏损，并用利润总额与减值金额交代量级），不要用倒算值撑场面。")
                issue_items.append({"t": "正文与表格的净利润对不上", "a": "blk-profit",
                                    "d": [{"num": num_in_body.group(1) + "亿元",
                                           "where": "盈利段", "kind": "写法",
                                           "full": num_in_body.group(0), "si": 0,
                                           "anchor": "blk-profit", "field": "pf-body",
                                           "note": "填进表，或改成定性表述"}]})
            else:
                issues.append(
                    "财务表「净利润」一整行都是「—」，正文只写了亏损。材料没给净利润金额时"
                    "不要编，但要把亏损的量级交代出来：用利润总额的同比降幅、减值损失的金额，"
                    "说明亏损是由哪几项堆出来的，读者才判断得了这是一次性亏损还是持续亏损。")
                issue_items.append({"t": "净利润有行无值", "a": "blk-fintable",
                                    "d": [{"num": "净利润 —", "where": "财务指标表",
                                           "kind": "结构",
                                           "full": "、".join(str(v) for v in np_row),
                                           "si": 0, "anchor": "blk-fintable", "field": "table",
                                           "note": "补不到金额就用利润总额与减值金额交代量级"}]})

    # 2) 正文数字溯源
    # 带段落标签收集，出问题时要能指出「在哪个段、哪句话、大概是哪个指标」——
    # 只报一串数字（14.76%、0.41亿元）等于让用户自己去全文搜，等于没提。
    # 第三项 meta 是定位信息：点一下要能落到具体那个输入框里的那一句，
    # 所以除了段落锚点，还要交代是哪个字段、第几个板块。
    labeled: list[tuple[str, str, dict]] = [
        ("基本情况", draft.get("basic_info", ""),
         {"anchor": "blk-basic", "field": "basic"})]
    c = draft.get("commentary") or {}
    for k, name in (("industry", "行业情况"), ("overview", "总述段"), ("asset", "资产段"),
                    ("profit", "盈利段"), ("debt", "债务段")):
        block = c.get(k) or {}
        field = {"industry": "in-body", "overview": "ov-body", "asset": "as-body",
                 "profit": "pf-body", "debt": "db-body"}[k]
        anchor = {"industry": "blk-industry", "overview": "blk-overview",
                  "asset": "blk-asset", "profit": "blk-profit", "debt": "blk-debt"}[k]
        labeled.append((name, (block.get("body") or ""),
                        {"anchor": anchor, "field": field}))
    for i, seg in enumerate(c.get("segments") or [], 1):
        labeled.append((f"{i}）{seg.get('name') or '板块'}", seg.get("body", ""),
                        {"anchor": "blk-segments", "field": "seg", "seg": i}))
    for i, t in enumerate(c.get("special_topics") or [], 1):
        labeled.append((f"专题段{i}", t.get("body", ""),
                        {"anchor": "blk-topics", "field": "topic", "seg": i}))
    texts = [t for _n, t, _m in labeled]
    seg_tbl = c.get("segment_table")
    if seg_tbl:
        for r in seg_tbl.get("rows") or []:
            texts.append(" ".join(str(x) for x in r))

    # 财务表逐格溯源：这一块以前完全在核对之外——正文里的数逐个查，表里的数却
    # 默认是可信的。于是「净利润」这种材料没给合并数的行，模型会拿子公司数或
    # 倒算值填进去，表看起来齐整，其实是编的。表格是读者最先看的地方，编在这里
    # 比编在正文里更糟，所以每一格都要能在材料里找到，或在同一列里算出来。
    fin_tbl = c.get("financial_table") or {}
    fin_bad: list[dict] = []
    _periods = [str(x) for x in (fin_tbl.get("periods") or [])]
    for ri, r in enumerate(fin_tbl.get("rows") or []):
        cells = [str(x) for x in (r or [])]
        if len(cells) < 2:
            continue
        rowname = cells[0].strip()
        col_vals: list[float] = []
        for v in cells[1:]:
            fv = _num_of(v)
            if fv is not None:
                col_vals.append(fv)
        for i, raw in enumerate(cells[1:]):
            v = _num_of(raw)
            if v is None:
                continue
            # 表格里的数带符号，不去绝对值比对：净利润 -8.39 若按 8.39 去材料里找，
            # 会命中「18.39%」这种毛利率，一笔编出来的负数就这么混过去了。
            hits = {f"{v:.2f}", f"{v:.1f}", f"{v:.0f}"}
            if hits & corpus_nums:
                continue
            # 这一格没在材料里出现，可能是同列算出来的（资产负债率、总资本化比率…）。
            # 用同一列的其他数反推：算得上就销账，算不上的才按「编的」报出去。
            pool = [x for x in col_vals if not _close(x, v)]
            tmpl = _formula_table(pool)
            kind = ("比率" if ("%" in raw or "率" in rowname or "比" in rowname)
                    else "倍数" if "倍" in rowname else "")
            if _try_formula(abs(v), kind, tmpl):
                continue
            period = _periods[i] if i < len(_periods) else f"第{i + 1}列"
            fin_bad.append({"num": raw, "where": f"财务指标表 · {rowname} · {period}",
                            "kind": "表格", "full": f"{rowname}（{period}）：{raw}",
                            "si": 0, "anchor": "blk-fintable", "field": "table",
                            "note": "材料里没有这一格，同列也推不出来，已先置为「—」"})
    # 找不到出处的格子直接置为「—」。这不是宁缺毋滥的美德，是这一处的实际教训：
    # 材料只写「计提减值导致净利润出现亏损」，模型却给出「净利润 -8.39 亿元」
    # （把子公司 75.28 抄成 75.29、再用净资产收益率倒算），表看起来齐整，
    # 数字却是凑的。内评表里一个凑出来的数比一个「—」危险得多——
    # 前者会被继续引用，后者会被人去问「这个怎么没有」。
    if fin_bad and fin_tbl.get("rows"):
        for b in fin_bad:
            parts = b["where"].split(" · ")
            if len(parts) < 3:
                continue
            nm, period = parts[1], parts[2]
            for r in fin_tbl["rows"]:
                if str(r[0]).strip() != nm:
                    continue
                for i, p in enumerate(_periods, 1):
                    if p == period and i < len(r):
                        r[i] = "—"
        b0 = fin_bad[0]
        sample = "；".join(f"{x['where']}＝{x['num']}" for x in fin_bad[:4])
        issues.append(
            f"财务指标表有 {len(fin_bad)} 格数在材料里找不到出处、同列也推不出来（{sample}），"
            "已先置为「—」。表格是读者最先看的地方，材料没给的项就该留空，"
            "不要用子公司数、净资产收益率倒算或相邻年份顶上；确实自己算的，"
            "把算式写进 data_notes，程序会复算后再填回来。")
        issue_items.append({"t": "财务表有数找不到出处", "a": "blk-fintable",
                            "d": fin_bad[:8]})
        draft["data_notes"] = list(draft.get("data_notes") or []) + [
            "财务表置空（材料未给出、同列推不出）：" + sample
            + ("……" if len(fin_bad) > 4 else "")]

        # 置空之后再看一眼正文：表里空了、正文却还写着净利润的具体金额，
        # 两处对不上。金额若真有依据，就该把它填回表里（并附算式）；
        # 若来路不明，正文要改成定性表述——不能一边留白一边报数。
        _np = next((r for r in (fin_tbl.get("rows") or [])
                    if str((r or [""])[0]).strip() and "净利润" in str(r[0])), None)
        if _np and all(_num_of(x) is None for x in list(_np)[1:]):
            nb = re.search(r"净利润[^，。；]{0,6}?(-?[\d,]+\.\d{2})\s*亿元", _all_body)
            if nb:
                issues.append(
                    f"正文写了「净利润 {nb.group(1)} 亿元」，财务表里这一行却是「—」，两处对不上。"
                    "金额有材料支撑就填进表里；没有就把正文改成定性表述（写明亏损，"
                    "用利润总额与减值金额交代量级），不要一边留白一边报一个查不到来源的数。")
                issue_items.append({"t": "正文与表格的净利润对不上", "a": "blk-profit",
                                    "d": [{"num": nb.group(1) + "亿元", "where": "盈利段",
                                           "kind": "写法", "full": nb.group(0), "si": 0,
                                           "anchor": "blk-profit", "field": "pf-body",
                                           "note": "填进表，或改成定性表述"}]})

    # 财务表里的数本身已经逐个溯源过（见上），所以它们是可信的操作数。
    # 正文里的「同比降 1.90%」常常要用到上一年的数，而那个数往往只出现在表里、
    # 不出现在同一段正文里——只按同段反推就会把它判成「算不出来」。
    # 把表内的数并成一个操作数池，专给占比、增速这类结构性算式用。
    tab_fin: dict = {}
    if fin_tbl.get("rows"):
        _fin_pool = [v for r in fin_tbl["rows"] for v in
                     (_num_of(x) for x in list(r)[1:]) if v is not None]
        tab_fin = _formula_table(_fin_pool)

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
    # 先按模型交底的算式复算一遍：算得上的数直接销账，不必再进待核对。
    notes_ok, notes_bad = _verify_notes(draft, corpus_nums)
    proven: list[tuple[float, dict]] = [
        (float(r["val"]), r) for r in notes_ok if r["val"] is not None]

    def _proven_for(v: float) -> dict | None:
        """这个数有没有交底过算式并且复算通过（数值比较，不用字符串取整）。"""
        for pv, r in proven:
            if _close(abs(v), pv):
                return r
        return None

    # 同段数字池：没有交底算式时，退一步用「这一段里的数」反推算式。
    # 先试都已对得上材料的（结论最硬），再试整段全部数字——
    # 操作数自己没溯源不要紧，它各自还会单独进待核对；这里要确认的只是
    # 「这个结果是算出来的，不是凭空写的」。同段只有一二十个数，凑巧成立的概率很低。
    for label, text, meta in labeled:
        sure: list[float] = []
        every: list[float] = []
        for n in collect_numbers(text or ""):
            dg = re.sub(r"[^\d.]", "", n)
            try:
                v = float(dg)
            except ValueError:
                continue
            every.append(v)
            if dg in corpus_nums or f"{v:.2f}" in corpus_nums or f"{v:.1f}" in corpus_nums:
                sure.append(v)
        tab_sure = _formula_table(sure)
        tab_every = _formula_table(every)
        for sent in _sentences(text or ""):
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
                kind = _guess_kind(sent, n)
                loc = _locate_sent(text, sent, n)
                hit = {"num": n, "where": label, "sent": _cite(sent, n),
                       "full": loc["sent"], "si": loc["si"], "kind": kind,
                       "anchor": meta.get("anchor"), "field": meta.get("field"),
                       "seg": meta.get("seg") or 0}
                # ① 模型交过算式且复算通过
                note = _proven_for(v)
                if note:
                    hit["formula"], hit["rule"] = note["expr"], "算式复算通过"
                    auto_ok.append(hit)
                    continue
                # ② 同段反推：运算过程就在这一段里摆着
                # 差额与合计只在数字旁边确实写着「增加／减少／合计」时才认。
                # 一整句里出现「下降」不算数——那句话讲的是另一个数（「利润总额
                # 同比下降87.05%，净利润-8.39亿元」），按整句放行就会让 -8.39
                # 被「13.49 − 5.11 = 8.39」验成通过，而这两项跟净利润毫无关系。
                ctx = sent[max(0, sent.find(n) - 8): sent.find(n) + len(n) + 8]
                allow = {"ratio", "rest", "growth", "times"}
                if re.search(r"增加|减少|上升|下降|提高|回落|扩大|收窄|增长|降幅|增幅|"
                             r"多|少|变动|差额|增量", ctx):
                    allow |= {"diff"}
                if re.search(r"合计|累计|总计|共计|小计", ctx):
                    allow.add("sum")
                # 正文报降幅时常写「下降1.90%」而不带负号，算式算出来却是 -1.90。
                # 两个方向都试一次，别的什么都不改。
                cands = [v]
                if re.search(r"下降|减少|降幅|回落|收窄|下滑|收窄", ctx):
                    cands.append(-v)
                proof = None
                _anchors = [float(re.sub(r"[^\d.]", "", x))
                            for x in collect_numbers(sent) if re.sub(r"[^\d.]", "", x)]
                # 先用句中的数配材料里的数；句子里的数不够用时，再放宽到整段。
                # 「量价齐跌使基础建材收入减少137.69亿元」这一句里没有 802.73，
                # 它在上一句，但仍是同一段的同一个话题。
                _para_anchors = [float(re.sub(r"[^\d.]", "", x))
                                 for x in collect_numbers(text or "")
                                 if re.sub(r"[^\d.]", "", x)]
                for cv in cands:
                    proof = (_try_formula(cv, kind, tab_sure, allow)
                             or _try_formula(cv, kind, tab_every, allow)
                             or _try_formula(cv, kind, tab_fin, allow)
                             or _try_formula_corpus(cv, kind, _anchors, corpus_nums, allow)
                             or _try_formula_corpus(cv, kind, _para_anchors, corpus_nums, allow))
                    if proof:
                        break
                if proof and not (digits in declared or f"{v:.2f}" in declared):
                    hit["formula"], hit["rule"] = proof[0], proof[1]
                    auto_ok.append(hit)
                    continue
                if digits in declared or f"{v:.2f}" in declared:
                    hit["formula"] = proof[0] if proof else ""
                    hit["rule"] = proof[1] if proof else "声明为推算值"
                    derived.append(hit)
                    continue
                unsourced.append(hit)

    if auto_ok:
        # 已核对的不进待核对，但要在页面上留个痕迹：不然用户会怀疑数字凭空少了
        by_rule: dict[str, int] = {}
        for h in auto_ok:
            by_rule[h.get("rule") or "其他"] = by_rule.get(h.get("rule") or "其他", 0) + 1
        auto_lines = [f"机器自动核对通过 {len(auto_ok)} 个自算值（"
                      + "、".join(f"{k} {v}" for k, v in by_rule.items()) + "）："]
        for h in auto_ok[:8]:
            auto_lines.append(f"· {h['num']} —— {h.get('formula') or '—'}　（{h['where']}）")
        if len(auto_ok) > 8:
            auto_lines.append(f"……另有 {len(auto_ok) - 8} 处")
        auto_notes.append("\n".join(auto_lines))
    if notes_bad:
        # 交了算式但对不上的，是最该看的一类：要么抄错数，要么算式写错。
        # 尽量按「算出来的值」在正文里找回那句话，点一下就能落过去。
        def _locate_val(val: float | None):
            if val is None:
                return None
            for label, text, meta in labeled:
                for sent in _sentences(text):
                    for n in collect_numbers(sent):
                        try:
                            nv = float(re.sub(r"[^\d.]", "", n))
                        except ValueError:
                            continue
                        if _close(abs(val), nv):
                            return {"where": label, "full": sent,
                                    "si": _index_of_sent(text, sent),
                                    "anchor": meta.get("anchor"),
                                    "field": meta.get("field"),
                                    "seg": meta.get("seg") or 0}
            return None

        issues.append(f"{len(notes_bad)} 条算式复算不通过（操作数在材料里找不到，或算出来与正文不符）")
        ndets = []
        for r in notes_bad[:8]:
            loc = _locate_val(r["val"]) or {}
            ndets.append({
                "num": (f"{r['val']:.2f}" if r["val"] is not None else "—"),
                "where": loc.get("where") or r["name"],
                "full": loc.get("full") or "", "si": loc.get("si") or 0,
                "formula": r["expr"], "kind": "算式",
                "note": (("算式本身算不出来（除零或写法有误）" if r["val"] is None else
                          ("操作数 " + "、".join(f"{m:g}" for m in r["missing"]) + " 在材料里找不到")
                          if r["missing"] else "算出来的值在正文里找不到")),
                "anchor": loc.get("anchor") or "d-doc",
                "field": loc.get("field") or "", "seg": loc.get("seg") or 0})
        issue_items.append({"t": "交底的算式复算不通过", "a": "d-doc", "d": ndets})
    if unsourced:
        # 明细不写进 issues 文本（前端要用结构化条目逐条定位），这里只留一句总述，
        # 具体「第几段第几句、哪个数」由 issue_items 承载。
        kinds = {}
        for h in unsourced:
            kinds[h["kind"] or "未定"] = kinds.get(h["kind"] or "未定", 0) + 1
        top = "、".join(f"{k} {v} 个" for k, v in sorted(kinds.items(), key=lambda x: -x[1])[:3])
        issues.append(f"{len(unsourced)} 个数字既对不上材料原文，也没能用同段数据复原算式"
                      f"（{top}），已逐条定位到句，请核对")
        issue_items.append({
            "t": "数字对不上，算式也复原不了", "a": "d-doc", "note": top,
            "d": [dict(x) for x in unsourced]})
    if derived:
        issues.append(f"{len(derived)} 个数字标为推算值，核对算式口径是否与正文一致")
        issue_items.append({
            "t": "推算值要说清口径", "a": "d-doc",
            "d": [dict(x) for x in derived]})

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
    bad = {p: (label, meta) for label, t, meta in labeled
           for p in (m.group(0) for m in pat.finditer(_norm_period(t or "")))
           if p not in allowed and p[:4] in years}
    if bad:
        issues.append("以下期次写法在台账与材料原文中找不到依据（可能是把材料里不同期次的数字拼到了同一个期次上），需核对："
                      + "、".join(sorted(bad)))
        issue_items.append({
            "t": "期次写法没有依据", "a": "d-doc",
            "d": [{"num": p, "where": bad[p][0], "anchor": bad[p][1].get("anchor"),
                   "field": bad[p][1].get("field"), "seg": bad[p][1].get("seg") or 0,
                   "full": "", "si": 0, "kind": "期次"}
                  for p in sorted(bad)]})

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
                issue_items.append({
                    "t": "资产负债表不平", "a": "blk-fintable",
                    "d": [{"num": f"{a - l - e:+.2f} 亿元", "where": f"财务表 {per}",
                           "anchor": "blk-fintable", "field": "table", "seg": 0,
                           "full": f"资产总额 {a:,.2f}、负债总额 {l:,.2f}、所有者权益 {e:,.2f}",
                           "si": 0, "kind": "差额", "note": "资产 − 负债 − 权益"}]})

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
    left = [(label, t, meta) for label, t, meta in labeled if has_view_words(t or "")]
    if left:
        sample = "；".join(_sketch(t) for _l, t, _m in left[:3])
        issues.append(f"正文仍有 {len(left)} 处交代资料来源的表述（程序未能自动改写），"
                      f"建议改为直接陈述或删去：{sample}")
        dets = []
        for label, t, meta in left[:8]:
            for sent in _sentences(t):
                if has_view_words(sent):
                    dets.append({"num": "来源表述", "where": label, "full": sent,
                                 "si": _index_of_sent(t, sent), "kind": "表述",
                                 "anchor": meta.get("anchor"), "field": meta.get("field"),
                                 "seg": meta.get("seg") or 0})
                    break
        issue_items.append({"t": "还有交代来源的句子", "a": "d-doc", "d": dets})

    # 7) 跨段重复：同一句话在别处已经说过。只提示不自动删——
    # 业务段的风险点与专题段的核心证据常常是同一事实，机器分不清该留哪处。
    named: list[tuple[str, str]] = [(label, text) for label, text, _m in labeled]
    seen_sent: list[tuple[str, str]] = []
    dup_hits: list[str] = []
    dup_dets: list[dict] = []
    for label, text in named:
        meta = next((m for l, _t, m in labeled if l == label), {})
        for sent in re.split(r"(?<=[。！？])", text):
            core = _core_text(sent)
            if len(core) < 20:
                continue
            for prev_label, prev_core in seen_sent:
                if is_real_dup(core, prev_core, 18, min_han=10):
                    dup_hits.append(f"「{sent.strip()[:26]}…」在{prev_label}与{label}重复")
                    if len(dup_dets) < 8:
                        dup_dets.append({"num": "重复句", "where": f"{label}（与{prev_label}重复）",
                                         "full": sent.strip(), "si": _index_of_sent(text, sent),
                                         "kind": "表述", "anchor": meta.get("anchor"),
                                         "field": meta.get("field"), "seg": meta.get("seg") or 0})
                    break
            else:
                seen_sent.append((label, core))
    if dup_hits:
        issues.append("同一表述在两处出现，删一处即可：" + "；".join(dup_hits[:3]))
        issue_items.append({"t": "同一句话出现了两遍", "a": "d-doc", "d": dup_dets})

    # 8) 程度词与过短板块：程度词留给数据去说，板块段要有实质内容
    vague = sorted({w for w in ("极大", "极强", "极弱", "恶劣", "严重恶化", "显著")
                    for t in texts if w in (t or "")})
    if vague:
        issues.append("正文含不可核实的程度词 " + "、".join(vague) + "，建议换成数据或分级判断词")
        vdets = []
        for label, t, meta in labeled:
            for w in vague:
                if w and w in (t or ""):
                    for sent in _sentences(t):
                        if w in sent:
                            vdets.append({"num": w, "where": label, "full": sent,
                                          "si": _index_of_sent(t, sent), "kind": "程度词",
                                          "anchor": meta.get("anchor"), "field": meta.get("field"),
                                          "seg": meta.get("seg") or 0})
                            break
                    break
        issue_items.append({"t": "程度词要换成数据", "a": "d-doc", "d": vdets[:8]})

    # 8.1) 点题式风险句：程序已把「该板块风险点在于」融进前句，剩下的变体人工看一眼
    #      （只认小句开头的点题，「回收风险主要来自…」这类定语说法不算）
    tagged: list[str] = []
    tdets: list[dict] = []
    for label, text, meta in labeled:
        for sent in re.split(r"(?<=[。；])", text or ""):
            if len(sent.strip()) > 6 and _RISK_TAG.search(sent):
                tagged.append(f"{label}「{sent.strip()[:26]}…」")
                if len(tdets) < 8:
                    tdets.append({"num": "点题句", "where": label, "full": sent.strip(),
                                  "si": _index_of_sent(text, sent), "kind": "表述",
                                  "anchor": meta.get("anchor"), "field": meta.get("field"),
                                  "seg": meta.get("seg") or 0})
    if tagged:
        issues.append("以下句子仍是在给风险贴标签（改成「条件—后果」，接在已经说过的事实后面，"
                      "不另起一句点题）：" + "；".join(tagged[:3]))
        issue_items.append({"t": "还在给风险贴标签", "a": "d-doc", "d": tdets})

    # 偏薄板块：带上原段序号，前端才能正确定位到那一段（不能再用子集里的序号）
    thin = [(i, s.get("name"), len((s.get("body") or "").strip()))
            for i, s in enumerate(c.get("segments") or [], 1)
            if 0 < len((s.get("body") or "").strip()) < 120]
    if thin:
        issues.append("以下板块段内容偏薄，补一句经营数据或制约条件（谁付钱、什么时候能回、"
                      "价格由谁定），或与同类小板块并为一段："
                      + "、".join(f"{n}（{k} 字）" for _i, n, k in thin))
        issue_items.append({
            "t": "板块段内容偏薄", "a": "blk-segments",
            "d": [{"num": f"{k} 字", "where": f"{i}）{n}", "full": "", "si": 0,
                   "kind": "篇幅", "anchor": "blk-segments", "field": "seg", "seg": i}
                  for i, n, k in thin]})

    # 8) 结构检查
    if len(draft.get("positive") or []) < 3:
        issues.append("正面不足 3 条")
        issue_items.append({"t": "正面不足 3 条", "a": "blk-pos", "d": []})
    if len(draft.get("concern") or []) < 3:
        issues.append("关注不足 3 条")
        issue_items.append({"t": "关注不足 3 条", "a": "blk-con", "d": []})
    if not (c.get("overview") or {}).get("lead"):
        issues.append("总述段缺加粗汇总句")
        issue_items.append({"t": "总述段缺加粗句", "a": "blk-overview",
                            "d": [{"num": "", "where": "总述段", "full": "", "si": 0,
                                   "kind": "结构", "anchor": "blk-overview",
                                   "field": "ov-lead", "seg": 0}]})
    if not (c.get("financial_table") or {}).get("rows"):
        issues.append("财务指标表为空（台账与材料里都没找到可用的财务数据）")
        issue_items.append({"t": "财务表是空的", "a": "blk-asset", "d": []})

    body_len = sum(len(t) for t in texts)
    if body_len < 2000:
        issues.append(f"正文偏短（约{body_len}字），模板要求 2000-4000 字")
    if body_len > 5200:
        issues.append(f"正文偏长（约{body_len}字），建议压缩到 4000 字以内")

    return {"issues": issues, "body_chars": body_len,
            "issue_items": issue_items,          # 结构化，带段/句定位
            "auto_checks": auto_notes,           # 机器已自己核对过的（附算式），仅留痕
            "auto_count": len(auto_ok),
            "rev": VALIDATOR_REV}                # 校验规则版本：变了就要重算存量记录


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
    # 期次列在这里就排好，而不是等到导出才排：页面上看到的表必须和 Word 里一致，
    # 否则用户改的是「2024年那一列」，导出后它会跑到最右边。
    draft["commentary"] = dict(draft.get("commentary") or {})
    for _k in ("financial_table", "segment_table"):
        if isinstance(draft["commentary"].get(_k), dict):
            draft["commentary"][_k] = reorder_table(draft["commentary"][_k])
    _p(progress, 96, "校验", "数字溯源、期次写法、财务表自洽")
    # 模型漏给「可补充段落」时用材料摘句兜底：提醒不到位，用户就不知道
    # 或有事项其实还有一段可以写。放在校验之前——校验也要看有没有建议。
    draft["add_on_sections"] = _clean_advice(
        merge_advice(draft.get("add_on_sections"), materials))
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
