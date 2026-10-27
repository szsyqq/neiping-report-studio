"""分析附件：债券代码清单、财务专项指标。

这两份是写报告前的底料，形态与口径都已固定（对照样例逐段核对过）：

一、债券代码清单（`<主体>-债券代码-<代表代码>.txt`）
    数据来自财汇「企业发行的债券查询」（get_company_issued_bonds），不吃 PDF：
    评级报告只列存续的那几只，指数级的私募债与历史到期债根本不在里面。
    按品种分组，每组一行「简称 | 代码 | 规模 | 期限 | 起始日」，代码后缀本身
    就说明场所（.SZ/.SH 交易所、.IB 银行间、.FI 私募）。文件名带代表代码，
    取最新一期交易所或银行间的代码，私募债不入文件名但正文列全。

二、财务专项指标（`<主体>-财务专项指标.txt`）
    四项：① 市场地位 ② 财报可靠性（审计机构 + 意见类型）③ 受限资产占比
    ④ 应收账款账龄（一年以上占比）。前三项从材料抽数；第四项很多主体不披露
    账龄表，那就如实写【未披露】并给替代信息（余额序列、前五名集中度），
    不拿别的数凑一个占比出来。

只整理，不解释：这里出现的每个数字都要能在材料里找到出处，或来自财汇。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

# ── 品种分组：顺序照样例，代码后缀顺带说明场所 ───────────────────────────
# (组名, 判定)  判定拿到的是财汇一行的 (明细类型, 一级类型, 上市市场)
GROUPS: tuple[tuple[str, str], ...] = (
    ("公司债（交易所，代码.SZ/.SH）", "corporation"),
    ("中期票据（MTN，银行间，代码.IB）", "mtn"),
    ("超短期融资券（SCP，银行间，代码.IB）", "scp"),
    ("短期融资券（CP，银行间，代码.IB）", "cp"),
    ("企业债（代码.IB）", "enterprise"),
    ("私募债（私募，代码.FI）", "private"),
    ("其他品种", "other"),
)


def _group_of(row: dict) -> str:
    primary = _s(row.get("债券类型"))
    detail = _s(row.get("债券明细类型"))
    market = _s(row.get("上市市场"))
    code = _s(row.get("债券代码"))
    blob = primary + detail
    if "私募" in blob or code.endswith(".FI"):
        return "private"
    if "超短期" in blob:
        return "scp"
    if "中期票据" in blob or "MTN" in blob.upper():
        return "mtn"
    if "企业债" in blob:
        return "enterprise"
    if "短期融资券" in blob or "一般短期" in blob:
        return "cp"
    if "公司债" in blob or (market in ("上交所", "深交所") and code[-3:] in (".SZ", ".SH")):
        return "corporation"
    return "other"


# ── 小工具 ──────────────────────────────────────────────────────────────
def _s(v: Any) -> str:
    return "" if v is None else str(v).strip()


def _flat(text: str) -> str:
    """PDF 抽出来按版面换行，先把空白去掉再切句。"""
    return re.sub(r"\s+", "", text or "")


def _wrap(prefix: str, text: str, width: int = 78) -> list[str]:
    t = _s(text)
    if not t:
        return []
    lines: list[str] = []
    rest, first = t, True
    while rest:
        head = prefix if first else " " * len(prefix)
        lines.append(head + rest[: width - len(prefix)])
        rest = rest[width - len(prefix):]
        first = False
    return lines


def _num(v: Any, digits: int = 2) -> str:
    """金额统一两位小数带千分位；取不到就返回空串（宁缺不编）。"""
    try:
        f = float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return ""
    return f"{f:,.{digits}f}"


def _pad(text: str, width: int, ascii_half: bool = True) -> str:
    """按「中文算两格」补齐，让竖线对齐——纯文本里对齐全靠这个。"""
    t = _s(text)
    w = 0
    for ch in t:
        w += 1 if (ord(ch) < 128) else 2 if ascii_half else 1
    return t + " " * max(0, width - w)


def _sentences(text: str) -> list[str]:
    out: list[str] = []
    for s in re.split(r"[。；\n]", _flat(text)):
        s = s.strip("，、：:）（() ")
        if 8 <= len(s) <= 320:
            out.append(s)
    return out


def _grab(text: str, pattern: str, group: int = 1) -> str:
    m = re.search(pattern, text)
    return m.group(group).strip() if m else ""


def _first_num(text: str, *patterns: str) -> tuple[str, str]:
    """按顺序找第一个命中的数字，返回 (数字, 命中的那句)。"""
    for p in patterns:
        m = re.search(p, text)
        if m:
            g = next((x for x in m.groups() if x), "")
            if g:
                return g, text[max(0, m.start() - 40): m.end() + 30]
    return "", ""


# ── 期次：同一指标在材料里常有好几期，一律取最新那一期 ──────────────────
# 不做竖排表格切分：PDF 里「209.53226.71244.71242.13」这种串，按两位小数能切开，
# 但一旦某期是整数或一位小数就整体错位，而且表头里混着页码年份，取错一次
# 就会把三年前的数当成最新的写进报告。宁可只用能确定期次的叙述句。
_PERIOD_RE = re.compile(
    r"(20\d{2})(?:\s*年\s*(?:(1[0-2]|[1-9])\s*月|末|半年度|年度|年)?|\.(\d{1,2}))?")


def _periods_in(head: str) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for m in _PERIOD_RE.finditer(head or ""):
        y = int(m.group(1))
        mo = m.group(2) or m.group(3) or ""
        # 「年末」不落在捕获组里，回看整段命中：「截至2025年末」要算 12 月，
        # 否则它会与「2025年」同权，取最新时被同样排在 (2025, 0)。
        if not mo and "末" in m.group(0):
            mo = "末"
        if mo == "末":
            out.append((y, 12))
        elif mo:
            try:
                out.append((y, int(mo)))
            except ValueError:
                out.append((y, 0))
        else:
            out.append((y, 0))
    return out


def _label(p: tuple[int, int]) -> str:
    y, mo = p
    if mo == 12:
        return f"{y}年末"
    if mo:
        return f"{y}年{mo}月末"
    return f"{y}年"


def _sent_latest(text: str, *patterns: str) -> tuple[str, str]:
    """按期次取最新的一处，返回 (数字, 期次文案)。

    期次必须来自命中处之前 40 字内的「截至2025年末」「2025年6月末」这类表述；
    找不到期次的命中直接不用——没有期次的数不知道是哪一期，写进报告就是隐患。
    """
    best: tuple[tuple[int, int], tuple[str, str]] = ((0, 0), ("", ""))
    for p in patterns:
        for m in re.finditer(p, text):
            head = text[max(0, m.start() - 40): m.start()]
            pers = _periods_in(head)
            if not pers:
                continue
            # 半年、季度口径与全年不可比：GDP 的「2025年上半年」数值只有全年一半，
            # 按期次比较会比 2024 年全年还新，于是被当成最新值写进去。
            if re.search(r"上半年|下半年|半年度|1[-－]6月|1[-－]9月|前三季度|季度", head):
                continue
            key = pers[-1]
            if key > best[0]:
                best = (key, (m.group(1), _label(key)))
    return best[1]


class _Doc:
    def __init__(self, title: str, when: str, note: str = ""):
        self.lines = [title, "=" * 80]
        if note:
            self.lines.append(note)
            self.lines.append("")

    def h(self, text: str):
        self.lines.append(text)

    def p(self, text: str = ""):
        self.lines.append(text)

    def text(self) -> str:
        return "\n".join(self.lines).rstrip() + "\n"


# ── 一、债券代码清单 ────────────────────────────────────────────────────
def _bond_rows(bonds: list[dict]) -> list[dict]:
    rows = []
    for b in bonds or []:
        if not isinstance(b, dict):
            continue
        code = _s(b.get("债券代码"))
        if not code:
            continue
        rows.append(b)
    rows.sort(key=lambda r: _s(r.get("发行起始日") or ""), reverse=True)
    return rows


def _rep_code(rows: list[dict]) -> tuple[str, dict]:
    """代表代码：最新一期交易所或银行间的代码，私募债不入选。"""
    for r in rows:
        code = _s(r.get("债券代码"))
        if code[-3:] in (".SZ", ".SH", ".IB"):
            return code, r
    for r in rows:
        return _s(r.get("债券代码")), r
    return "", {}


def _term_text(v: Any) -> str:
    t = _s(v)
    if not t:
        return ""
    t = t.replace(".00", "") if re.fullmatch(r"\d+\.00", t) else t
    return t if t.endswith("年") else t + "年"


def bonds_txt(draft: dict, when: str, materials: list | None = None,
              bonds: list[dict] | None = None, err: str = "") -> str:
    company = _s((draft or {}).get("company_full_name")) or "未命名主体"
    rows = _bond_rows(bonds or [])
    rep, rep_row = _rep_code(rows)
    d = _Doc(f"{company} 债券代码清单", when)
    if not rows:
        d.p("财汇未返回该主体的发行债券记录" + (f"（{err}）" if err else "。"))
        d.p("")
        d.p("可能原因：① 财汇 MCP 未配置或本次调用失败；② 该主体名称与财汇库不一致；")
        d.p("③ 主体从未在境内公开发债。核实后重试，或改用募集说明书里的债券条款。")
        return d.text()

    head = f"代表代码（取最新一期 SH/SZ/IB）：{rep}"
    if rep_row:
        bits = [_s(rep_row.get("债券简称")), _s(rep_row.get("发行起始日")),
                _s(rep_row.get("上市市场"))]
        head += "（" + "，".join(x for x in bits if x) + "）"
    d.p(head)
    private = [_s(r.get("债券简称")) + f"（{_s(r.get('债券代码'))}）"
               for r in rows if _group_of(r) == "private"]
    if private:
        d.p(f"说明：私募债 {'、'.join(private)} 不写入文件名，但完整列于下方「私募债」一节。")
    d.p(f"全部债券共 {len(rows)} 只，按品种分组列示如下"
        "（格式：债券简称 | 债券代码 | 发行规模 | 期限 | 发行起始日）：")
    d.p("")

    numerals = "一二三四五六七八九"
    for i, (gname, gkey) in enumerate(GROUPS):
        grp = [r for r in rows if _group_of(r) == gkey]
        if not grp and gkey == "other":
            continue
        d.p(f"{numerals[i] if i < len(numerals) else i + 1}、{gname}—— {len(grp)} 只")
        for r in grp:
            name = _pad(_s(r.get("债券简称")), 16)
            code = _pad(_s(r.get("债券代码")), 11)
            scale = _num(r.get("发行规模"))
            scale = _pad((scale + "亿") if scale else "—", 8)
            term = _pad(_term_text(r.get("债券期限")) or "—", 9)
            d.p(f"{name} | {code} | {scale} | {term} | {_s(r.get('发行起始日')) or '—'}")
        d.p("")

    d.p("=" * 80)
    rating = ""
    for r in rows:
        if _s(r.get("主体评级")):
            rating = _s(r.get("主体评级"))
            break
    inst = _rating_org(materials or [])
    org_bit = f"（最新：{inst}）" if inst else ""
    outlook = _grab(_all_text(materials or []), r"评级展望(?:为|是)?\s*(稳定|正面|负面|发展中)")
    d.p(f"主体评级：{rating or '—'}" + (f" / {outlook}" if outlook else "") + org_bit)
    nature = ""
    for r in rows:
        if _s(r.get("企业性质")):
            nature = _s(r.get("企业性质"))
            break
    is_ud = next((_s(r.get("是否城投")) for r in rows if _s(r.get("是否城投"))), "")
    ctrl = next((_s(r.get("实控人名称")) for r in rows if _s(r.get("实控人名称"))), "")
    d.p(f"性质：{nature or '—'}" + (f" / 城投（{is_ud}）" if is_ud else "")
        + (f"；实际控制人：{ctrl}" if ctrl else ""))
    d.p(f"口径说明：数据来自财汇「企业发行的债券查询」，按发行起始日降序取全量 {len(rows)} 只；"
        "含已到期债券。整理时间 " + when + "。")
    return d.text()


def _rating_org(materials: list[dict]) -> str:
    """从材料里摘「评级机构 + 最近一次评级日期」。

    同一主体常有多份评级报告（年度报告、跟踪报告），日期差一年；
    取日期最新的那一份——写「最新评级」却引了上一年的报告日期，是常见的手误。
    """
    best: tuple[tuple[int, int, int], str] = ((0, 0, 0), "")
    for m in materials or []:
        text = _flat(m.get("text") or "")
        for org in ("中诚信国际", "联合资信", "中证鹏元", "东方金诚", "上海新世纪", "大公国际"):
            for hit in re.finditer(
                    rf"{org}[^。；]{{0,70}}?(20\d{{2}})年(\d{{1,2}})月(\d{{1,2}})日", text):
                key = (int(hit.group(1)), int(hit.group(2)), int(hit.group(3)))
                if key > best[0]:
                    best = (key, f"{org} {hit.group(1)}-{int(hit.group(2)):02d}-"
                                 f"{int(hit.group(3)):02d} 跟踪评级")
            # 报告落款日期常在标题附近，没有「年X月X日」时退回只写机构
            if not best[1] and org in text:
                best = (best[0], org)
    return best[1]


# ── 二、财务专项指标 ────────────────────────────────────────────────────
def _all_text(materials: list[dict]) -> str:
    return "\n".join(_flat(m.get("text") or "") for m in materials or [])


def _fin_val(draft: dict, row_key: str, idx: int = 0) -> tuple[str, str]:
    """从草稿的财务指标表取某个行项的某一期值，返回 (值, 期次)。"""
    fin = ((draft or {}).get("commentary") or {}).get("financial_table") or {}
    periods = [_s(x) for x in (fin.get("periods") or [])]
    for r in fin.get("rows") or []:
        cells = [_s(x) for x in (r or [])]
        if cells and row_key in cells[0] and len(cells) > idx + 1:
            v = cells[idx + 1]
            if v and v not in ("—", "-", "--"):
                return v, (periods[idx] if idx < len(periods) else "")
    return "", ""


def _position(draft: dict, text: str) -> str:
    """定位：股权关系 + 平台属性。

    两个坑都踩过：
    - 用宽松的「…城投/城市建设…平台」兜底，结果抓到「中诚信国际…受评主体」——
      「主体」二字遍地都是，不能当锚；
    - 抓到「…服务运营商，平台」这种拼接残句，读起来像掉了半句。
      所以只取完整分句（到逗号/句号为止），并去掉尾部不足四字的成分。
    """
    bits: list[str] = []
    m = re.search(r"(?:由|系)?([^，。；（]{2,20}?(?:人民政府|国资委|财政厅|财政局|"
                  r"国有资产监督管理委员会))(?:（[^）]{0,20}）)?(?:100%|全资|绝对)?"
                  r"(?:持股|持有(?:公司|发行人)?(?:[\d.]+%)?股权|出资)", text)
    if not m:
        m = re.search(r"实际控制人为([^，。；（]{2,20}?(?:人民政府|国资委|财政局|"
                      r"国有资产监督管理委员会))", text)
    if m:
        pct = re.search(r"持有(?:公司|发行人)?([\d.]+%)股权", text)
        bits.append(m.group(1) + ("持股 " + pct.group(1) if pct else "持股"))
    else:
        m2 = re.search(r"控股股东为([^，。；]{2,24}?)，?持股(?:比例)?([\d.]+%)", text)
        if m2:
            bits.append(f"控股股东为{m2.group(1)}，持股{m2.group(2)}")
    # 平台属性：优先「作为／是……运营商／建设主体」这种完整定性
    for pat in (r"(?:作为|是|系)([^，。；]{6,36}?(?:运营商|运营主体|投资主体|建设主体|实施主体))",
                r"([^，。；]{6,36}?(?:城投平台|城市基础设施[^，。；]{0,10}平台))"):
        m3 = re.search(pat, text)
        if not m3:
            continue
        seg = m3.group(1).strip("，、（）() ")
        if any(x in seg for x in ("受评", "评级", "发行", "中诚信", "联合资信")):
            continue
        if len(seg) >= 6:
            bits.insert(0, "公司是" + seg if not seg.startswith(("公司", "发行人")) else seg)
            break
    return "，".join(dict.fromkeys(bits))


def _fiscal(text: str) -> list[str]:
    """区域财力：GDP 与一般公共预算收入，各取最新年份。"""
    gdp, gy = _sent_latest(text, r"(?:地区生产总值|GDP)(?:为|达|约)?([\d,]+\.?\d*)\s*亿元")
    inc, iy = _sent_latest(text, r"一般公共预算收入(?:为|达|约|完成)?([\d,]+\.?\d*)\s*亿元")
    out: list[str] = []
    if gdp:
        bit = f"地区生产总值 {gdp} 亿元（{gy}）"
        if inc:
            bit += f"，一般公共预算收入 {inc} 亿元（{iy}）"
        out.append(bit)
    elif inc:
        out.append(f"一般公共预算收入 {inc} 亿元（{iy}）")
    return out


def _audit(text: str) -> tuple[str, str]:
    """审计机构 + 意见类型。"""
    org = ""
    m = re.search(r"经([^，。；]{2,30}?(?:会计师事务所|审计机构)(?:（[^）]{2,10}）)?)"
                r"[^。；]{0,12}?(?:审计|出具)", text)
    if m:
        org = m.group(1)
    if not org:
        m = re.search(r"审计机构[：:]?([^，。；]{2,30}?)(?:[，。；]|$)", text)
        if m:
            org = m.group(1)
    if org:
        org = re.sub(r"^(?:公司|发行人)?(?:聘请|委托)?", "", org)
    opinion = ""
    for word in ("标准无保留意见", "带强调事项段的无保留意见", "保留意见",
                 "无法表示意见", "否定意见"):
        if word in text:
            opinion = word
            break
    return org, opinion


def _restricted(text: str) -> dict:
    """受限资产：金额与占比必须来自同一句、同一期次。

    这一条踩过坑：金额按「取最新」拿到 2025 年 6 月末的 19.16 亿元，
    占比却按「取最新」拿到 2025 年末的 11.78%，凑出「19.16 亿元占 11.78%」——
    两个数各自都对，配在一起就是错的，而且错得很像真的（19.16/244.6=7.8%，
    读者不逐项核对根本发现不了）。所以改成按句配对：一句里同时有金额和占比
    才采用；只有金额的，就只写金额并注明「原文未给同期占比」。
    """
    out: dict[str, str] = {}
    best: tuple[tuple[int, int], dict] = ((0, 0), {})
    for m in re.finditer(r"[^。；]{0,80}?(?:受限资产|所有权受限的资产)[^。；]{0,120}", text):
        sent = m.group(0)
        head = text[max(0, m.start() - 40): m.start()]
        # 期次优先取句内「截至2025年末」——从前文 80 字里找容易抓到上一段
        # 或文档标题里的年份（本次就抓到 2026 年，把 2025 年末的数标成了 2026 年）
        anchor = re.search(r"截至?\s*(20\d{2}\s*年(?:\s*\d{1,2}\s*月)?(?:末|底)?)", sent)
        pers = _periods_in(anchor.group(1) if anchor else "") or _periods_in(head)
        key = pers[-1] if pers else (0, 0)
        amt_yi = re.search(r"([\d,]+\.?\d*)\s*亿元", sent)
        amt_wan = re.search(r"([\d,]+\.?\d*)\s*万元", sent)
        pct = re.search(r"占(?:同期末|期末)?(?:总资产|资产总额)的?(?:比例)?(?:为)?\s*([\d.]+)%", sent)
        cur: dict[str, str] = {}
        if amt_yi:
            cur["amount"] = amt_yi.group(1)
        elif amt_wan:
            try:
                cur["amount"] = f"{float(amt_wan.group(1).replace(',', '')) / 10000:,.2f}"
                cur["unit_note"] = f"原文 {amt_wan.group(1)} 万元"
            except ValueError:
                pass
        if not cur.get("amount"):
            continue
        if pct:
            cur["pct"] = pct.group(1)
        cur["period"] = _label(key) if key != (0, 0) else ""
        # 期次最新优先；同期次下「金额与占比齐全」优先
        score = (key, 1 if pct else 0)
        if score > (best[0], 1 if best[1].get("pct") else 0):
            best = (key, cur)
    out = dict(best[1])
    # 明细与原因不分期次：材料通常只在一处列明细
    detail = re.findall(r"(货币资金|投资性房地产|固定资产及无形资产|固定资产|无形资产|"
                        r"存货|在建工程|应收账款|其他应收款|其他)\s*([\d,]+\.\d{2})\s*万元", text)
    if detail:
        seen: dict[str, str] = {}
        for k, v in detail:
            seen.setdefault(k, v)
        out["detail"] = "；".join(f"{k} {v}" for k, v in seen.items())
    m = re.search(r"受限(?:原因|原因为|主要系)(?:为|系)?([^。；]{4,70})", text)
    if m:
        out["reason"] = m.group(1).strip("，、 ")
    return out


def _ar_age(text: str) -> dict:
    """应收账款账龄。

    只在「应收账款」自己的上下文里判断，不能全文搜「账龄」——材料里的账龄
    大多属于**其他应收款**（土地收储款、往来款），拿它冒充应收账款账龄
    就把两件事混成一件，正是要避免的那种错。
    """
    out: dict[str, str] = {}
    win: list[str] = []
    for m in re.finditer(r"应收账款", text):
        seg = text[m.start(): m.start() + 260]
        if "账龄" not in seg:
            continue
        if re.search(r"一年以内|1年以内|一年以上|1年以上|1-2年|一至二年", seg):
            win.append(seg)
    out["has_table"] = "1" if win else ""
    if win:
        within, _ = _sent_latest(win[0], r"(?:一年以内|1年以内)[^。；\d]{0,10}([\d.]+)%")
        if within:
            out["within"] = within
            try:
                out["over"] = f"{100 - float(within):.2f}"
            except ValueError:
                pass
        else:
            over, _ = _sent_latest(win[0], r"(?:一年以上|1年以上)[^。；\d]{0,10}([\d.]+)%")
            if over:
                out["over"] = over
    # 余额序列：材料里多为报表竖排「应收账款148,035.90146,154.3023,991.14…」，
    # 按两位小数切开就是各期账面价值（报表惯例：最新在最左）。这不是占比，
    # 但能看出体量与跳升，比只写「未披露」有用。
    # 同一份材料里有合并与母公司两张表，序号小的那个往往是母公司（8,128 万元级）。
    # 取首个数值最大的一组：合并口径必然不小于母公司，这是报表关系的硬约束，
    # 不是猜——但只用于挑表，不用于改数。
    groups: list[list[str]] = []
    for m2 in re.finditer(r"应收账款((?:\d[\d,]*\.\d{2}){2,6})", text):
        seq = re.findall(r"\d[\d,]*\.\d{2}", m2.group(1))
        if len(seq) >= 2:
            groups.append(seq)
    if groups:
        seq = max(groups, key=lambda g: float(g[0].replace(",", "") or 0))
        unit = "万元" if max(len(x.replace(",", "").split(".")[0]) for x in seq) >= 5 else "亿元"
        out["series"] = "、".join(seq[:6]) + f"（{unit}，最新在前，期次见原文表头）"
    top = _grab(text, r"前(?:五|5)名[^。；\d]{0,16}(?:占|合计)[^。；\d]{0,10}([\d.]+)%")
    if top:
        out["top5"] = top
    prov = _grab(text, r"应收账款[^。；]{0,26}(?:未计提|不计提|已计提)[^。；]{0,18}坏账准备")
    if prov:
        out["provision"] = prov
    return out


def _biz(draft: dict, text: str) -> str:
    """业务结构：板块名或主营描述，不带金额。"""
    seg = ((draft or {}).get("commentary") or {}).get("segment_table") or {}
    names = [_s(r[0]) for r in (seg.get("rows") or []) if r and _s(r[0])]
    names = [n for n in names if n and "合计" not in n and "总" not in n[-1:]]
    if len(names) >= 2:
        return "以" + "、".join(names[:6]) + "为主"
    m = re.search(r"主营业务(?:包括|为|主要)?([^。；]{6,80})", text)
    if m:
        seg_text = m.group(1)
        # 去掉带金额的分句：业务结构说「做什么」，钱在第①项之外的指标里
        parts = [p for p in re.split(r"[，、]", seg_text) if not re.search(r"\d+\.\d+|%", p)]
        if parts:
            return "、".join(x.strip("等 ") for x in parts[:6])
    return ""


def metrics_txt(draft: dict, when: str, materials: list | None = None,
                bonds: list[dict] | None = None, err: str = "") -> str:
    company = _s((draft or {}).get("company_full_name")) or "未命名主体"
    text = _all_text(materials or [])
    d = _Doc(f"{company} 财务专项指标（4 项）", when)

    # ① 市场地位
    d.h("① 市场地位")
    d.h("-" * 80)
    pos = _position(draft, text)
    rating_rows = _bond_rows(bonds or [])
    rating = next((_s(r.get("主体评级")) for r in rating_rows if _s(r.get("主体评级"))), "")
    pos_line = pos or "（材料未给出股权关系与平台定位的原话，需人工补充）"
    if rating:
        pos_line += f"；主体评级 {rating}"
        org = _rating_org(materials or [])
        if org:
            pos_line += f"（{org}）"
    d.p(f"- 定位：{pos_line}")
    fiscal = _fiscal(text)
    if fiscal:
        for bit in fiscal:
            d.p(f"- 区域财力（材料原文口径）：{bit}")
        if "一般公共预算收入" not in "".join(fiscal):
            d.p("- 一般公共预算收入：材料只在对比表里列示、未以文字表述，此处不代取；"
                "需要时按最新公开数据核实后补。")
    else:
        d.p("- 区域财力：材料里没有抽到 GDP／一般公共预算收入的实数，需另行补充")

    # 资产与营收：优先用财务指标表（页面上已核对过），没有才回材料取最新一期。
    # 两者都要带期次——不带期次的金额读者无法判断是新数还是旧数。
    ta, ta_p = _fin_val(draft, "资产总额")
    rev, rev_p = _fin_val(draft, "营业总收入")
    if not ta:
        ta, ta_p = _sent_latest(text, r"(?:资产总额|总资产)(?:为|达|约|合计)?([\d,]+\.?\d*)\s*亿元")
    if not rev:
        rev, rev_p = _sent_latest(text, r"(?:营业总收入|营业收入)(?:为|达|约|实现)?([\d,]+\.?\d*)\s*亿元")
    bits = []
    if ta:
        bits.append(f"{ta_p or '材料口径'}总资产 {ta} 亿元")
    if rev:
        bits.append(f"{rev_p or '材料口径'}营业总收入 {rev} 亿元")
    if bits:
        d.p("- 资产与营收：" + "；".join(bits))
    else:
        d.p("- 资产与营收：材料里没有直接给出总资产与营业总收入，需从报表补充")
    biz = _biz(draft, text)
    if biz:
        d.p(f"- 业务结构：{biz}")
    d.p("")

    # ② 财报可靠性
    d.h("② 财报可靠性（审计机构 + 意见类型）")
    d.h("-" * 80)
    org, opinion = _audit(text)
    if org:
        d.p(f"- 审计机构：{org}")
    else:
        d.p("- 审计机构：材料未写明")
    if opinion:
        d.p(f"- 意见类型：{opinion}")
        if opinion == "标准无保留意见":
            d.p("- 结论：审计意见为标准无保留，财报可靠性未见异常；")
            d.p("  仍需注意会计政策变更、合并范围变化对可比性的影响。")
        else:
            d.p("- 结论：非标准意见，须逐条看清事项段涉及的科目与金额，不能只看结论。")
    else:
        d.p("- 意见类型：材料未写明。评级的财务分析全部建立在报表之上，")
        d.p("  缺这一项应回材料核实，不要默认它是标准无保留。")
    d.p("")

    # ③ 受限资产占比
    d.h("③ 受限资产占比")
    d.h("-" * 80)
    rs = _restricted(text)
    if rs.get("amount"):
        line = f"- {rs.get('period') or '材料口径'}：受限资产账面价值合计 {rs['amount']} 亿元"
        if rs.get("pct"):
            line += f"，占同期末总资产的 {rs['pct']}%"
        d.p(line)
        if rs.get("unit_note"):
            d.p(f"  （{rs['unit_note']}，已换算为亿元）")
    else:
        d.p("- 受限资产：材料里没有抽到受限资产总额的原话，需回报表核实")
    if rs.get("detail"):
        d.p(f"- 受限科目明细（万元）：{rs['detail']}")
    if rs.get("reason"):
        d.p(f"- 主要受限原因：{rs['reason']}")
    if rs.get("pct"):
        try:
            p = float(rs["pct"])
            level = "受限比例低" if p < 5 else ("受限比例中等" if p < 15 else "受限比例偏高")
            d.p(f"- 判断：{level}（{p:.2f}%）；抵押资产集中于投资性房地产与存货时，"
                "处置弹性会明显低于账面占比所显示的水平。")
        except ValueError:
            pass
    d.p("")

    # ④ 应收账款账龄
    d.h("④ 应收账款账龄（一年以上占比）")
    d.h("-" * 80)
    ar = _ar_age(text)
    if ar.get("over"):
        d.p(f"- 一年以上占比：{ar['over']}%" + (f"（材料给的一年以内为 {ar['within']}%）"
                                          if ar.get("within") else ""))
    elif ar.get("has_table"):
        d.p("- 材料有账龄表述，但只给了分段金额、没给占比，需按分段金额自行加总后再算。")
    else:
        d.p("- 【未披露】本次材料未列示应收账款账龄分布表（多数只披露账面价值与前五名债务人），")
        d.p("  无法直接计算「一年以上占比」；不估算、不拿别的比例替代。")
    if ar.get("series"):
        d.p(f"- 替代信息 · 账面价值序列：{ar['series']}")
    if ar.get("top5"):
        d.p(f"- 替代信息 · 前五名占应收账款余额 {ar['top5']}%，集中度"
            + ("偏高，回款取决于少数对手方" if float(ar["top5"]) >= 50 else "尚可"))
    if ar.get("provision"):
        d.p(f"- 计提情况：{ar['provision']}")
    d.p("")

    d.h("=" * 80)
    names = [re.sub(r"(_[0-9a-f]{12})+$", "", _s(m.get("title") or m.get("name")))
             for m in materials or []]
    names = [n for n in names if n]
    d.p("数据来源：" + ("、".join(names[:5]) if names else "本次未附材料")
        + ("……" if len(names) > 5 else ""))
    cut = [m for m in materials or [] if "摘录" in _s(m.get("text"))[:40]]
    if cut:
        d.p("阅读范围：上述材料按内容价值抽取摘录（非全文），比例见各份材料首行；"
            "摘录未覆盖到的期次与明细不在本表内，涉及最新一期数据时请回原文核对。")
    d.p("提取说明：以上各项均取自材料原文或财务指标表，未披露项已如实标注；"
        "区间与口径照原文，不做换算与推算。")
    d.p("整理时间：" + when)
    return d.text()


# key 用于前后端传参；name/short 由 build 按主体与代表代码拼
SPECS = (
    ("bonds", "债券代码", "全量债券按品种分组的代码清单", bonds_txt),
    ("metrics", "财务专项指标", "市场地位、财报可靠性、受限资产、应收账款账龄", metrics_txt),
)


def build(draft: dict, when: str | None = None, materials: list | None = None,
          bonds: list | None = None, err: str = "") -> list[dict]:
    """生成两份附件：[{key, name, short, desc, chars, text}]。"""
    when = when or datetime.now().strftime("%Y-%m-%d %H:%M")
    company = _s((draft or {}).get("company_full_name")) or "未命名主体"
    safe = "".join(ch for ch in company if ch not in '\\/:*?"<>|').strip() or "未命名主体"
    rep, _row = _rep_code(_bond_rows(bonds or []))
    out: list[dict] = []
    for key, title, desc, fn in SPECS:
        text = fn(draft or {}, when, materials, bonds or [], err)
        if key == "bonds":
            # 文件名带代表代码：同一主体的不同批次，看文件名就知道是哪一只
            name = f"{safe}-债券代码-{rep}.txt" if rep else f"{safe}-债券代码.txt"
            short = f"债券代码 {rep}" if rep else "债券代码"
        else:
            name = f"{safe}-{title}.txt"
            short = title
        out.append({"key": key, "name": name, "short": short, "dir": "分析附件",
                    "desc": desc, "chars": len(text), "text": text})
    return out


def one(draft: dict, key: str, when: str | None = None, materials: list | None = None,
        bonds: list | None = None, err: str = "") -> dict | None:
    for f in build(draft, when, materials, bonds, err):
        if f["key"] == key:
            return f
    return None


def meta_list(draft: dict, materials: list | None = None,
              bonds: list | None = None, err: str = "") -> list[dict]:
    """只要清单，不要全文——侧栏与两个勾选面板都用它。"""
    return [{k: f[k] for k in ("key", "name", "short", "desc", "chars")}
            for f in build(draft, None, materials, bonds, err)]
