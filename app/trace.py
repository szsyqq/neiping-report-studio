"""数字溯源：正文里的每个数，回到它来的地方。

一件事要回答三层：

1. **这个数从哪来**——在材料原文里找同一个值，指出是哪份文件、第几页、原话怎么写的。
   找不到就说找不到，不含糊。
2. **算出来的数怎么算的**——先看模型在 `data_notes` 里交底的算式（它自己写的，
   比事后反推可信），照着算一遍；没交底的再用同一段的数字穷举反推，但要说清
   「可能是这两个数算的」，不能假装是确定的。
3. **算式的每个原数又从哪来**——操作数逐个回到材料，形成一条能走到底的链。

关键取舍：

- **只用摘录里的材料做索引**。生成时进提示词的就是摘录，模型能写出来的数只能来自
  那里；拿全文去搜会让「材料里有、模型没看到」的数也标成已溯源，那是假溯源。
  真要翻全文，走 `deep_search` 单独一次，明确告诉用户「这一页没进过模型」。
- **值比对而不是字符串比对**。正文写「8.08亿元」，材料写「808,000,000.00」，
  按字面永远对不上。这里按单位换算成同一量纲的候选值再比。
- **上下文一致性用来排序，不用来判定**。同一个值在材料里出现十次（合并/母公司、
  本期/上期），挑最相关的那处给人看，其余列在后面——由人判断是哪一个。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

import generator as G

# 数字本体：允许千分位与小数。单位在右侧另行判断，不并进正则——
# 并进去的话，「8.08 亿元」（中间有空格）会漏，「8.08亿」又会被截成 8.08。
_NUM = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
_PAGE_MARK = re.compile(r"\[第(\d+)页\]")

# 右侧单位，长单位在前：先匹配「个百分点」再匹配「%」，否则「3.21 个百分点」
# 会被认成「3.21」加一个没解释的后缀。
_UNITS = ("个百分点", "亿元", "万元", "亿元", "bps", "BP", "bp", "%", "％", "倍", "元")

# 后接这些字的数字是「期次」或「篇幅」，不是要核的量。它们不是数据，
# 混进溯源率里会让一份好稿看起来只有六成溯源。
_META_TAIL = ("页", "秒", "分钟", "字", "个交易日", "年", "月", "日")


def _key(v: float, nd: int = 2) -> str:
    """数值归一化成索引键。四舍五入到两位小数，正好对应报告与材料的小数位习惯。"""
    try:
        return f"{abs(float(v)):.{nd}f}"
    except (TypeError, ValueError):
        return ""


def _to_float(s: str) -> float | None:
    try:
        return float(re.sub(r"[^\d.\-]", "", s))
    except (TypeError, ValueError):
        return None


# ── 单位换算：同一个量在不同材料里的几种写法 ────────────────────────────
def cand_values(v: float, unit: str) -> list[float]:
    """把正文里的一个数换算成材料里可能出现的写法。

    亿元 / 万元 / 元三种口径在同一套材料里混用是常态：评级报告写「8.08 亿元」，
    财务报表写「808,000,000.00」。不做换算，这类数字全部会被判为「没有出处」。

    换出来的值小于 1000 的一律丢掉：0.05 亿元确实是 500 元，但材料里「500」
    这个数到处都是，收进来只会制造假命中——一条「有出处」的错误结论，比
    「没找到」更耽误事。百分比不做 ÷100 同理：中文财务表习惯直接写 14.76，
    写成 0.1476 的极少，而 0.03 这类小数在附注里遍地都是。
    """
    v = abs(v)
    out = [v]
    if unit == "亿元":
        out += [v * 1e4, v * 1e8]
    elif unit == "万元":
        out += [v * 1e4, v / 1e4]
    elif unit == "元":
        out += [v / 1e4, v / 1e8]
    return [x for x in out if x >= 1000 or x == v]


def _norm_unit(u: str) -> str:
    return "个百分点" if u == "个百分点" else ("%" if u in ("%", "％") else u)


# ── 材料索引 ──────────────────────────────────────────────────────────
class MatIndex:
    """把每份材料的数字一次性扫进一张表，之后按值查位置。

    正文动辄两三百个数，逐个回材料全文搜是 O(数字 × 材料长度)，两千字的材料
    也要几百毫秒；建一次索引是 O(材料长度)，之后每次查询只走字典。
    """

    def __init__(self) -> None:
        self.docs: list[dict] = []
        self.by_val: dict[str, list[tuple[int, int, int, str]]] = {}
        self.by_val0: dict[str, list[tuple[int, int, int, str]]] = {}

    def add(self, title: str, text: str, path: str = "", pages: int = 0) -> None:
        di = len(self.docs)
        body = text or ""
        self.docs.append({"i": di, "title": title or "", "path": path,
                          "pages": pages or 0, "text": body})
        # 页标记本身不是数据：`[第12页]` 里的 12 不能进索引，
        # 否则正文写「12 页」时会查到它，看着像有出处。
        marks = [(m.start(), m.end(), int(m.group(1))) for m in _PAGE_MARK.finditer(body)]
        mark_at = {i for a, b, _ in marks for i in range(a, b)}
        page_now, mp = 0, 0
        for m in _NUM.finditer(body):
            if m.start() in mark_at:
                continue
            # 列表编号不是数据。"20、关联交易较多的风险" 里的 20，在按值查的时候
            # 就是「20.00 亿元」的候选值，一撞一个准——这种假命中比查不到更糟：
            # 它会把一个正确的数指向一句毫不相干的原文，看着像核对过了。
            tail1 = body[m.end(): m.end() + 1]
            if tail1 in ("、", "）", ")"):
                continue
            while mp < len(marks) and marks[mp][1] <= m.start():
                page_now = marks[mp][2]
                mp += 1
            v = _to_float(m.group(0))
            if v is None:
                continue
            # 记下材料里这个数后面跟的单位：正文写「3.21%」，材料里那个 3.21 是
            # 「3.21 亿元」，两者不是一回事，靠这一位把金额与比率分开。
            tail = body[m.end(): m.end() + 6]
            mu = ""
            for x in _UNITS:
                if tail.startswith(x):
                    mu = _norm_unit(x)
                    break
            rec = (di, m.start(), page_now, mu)
            self.by_val.setdefault(_key(v, 2), []).append(rec)
            self.by_val0.setdefault(_key(v, 0), []).append(rec)

    def lookup(self, v: float) -> list[tuple[int, int, int, str]]:
        """按值找位置。

        两位小数匹配不上时，只有在数值够大（≥1000）才退到整数位：百万级的数
        退一位仍是唯一的那一个（808,000,000 不会撞上 808,000,001），而 3.21
        退成 3 就会撞上材料里所有的 3，那是假命中。
        """
        hit = self.by_val.get(_key(v, 2))
        if hit:
            return hit
        if abs(v) >= 1000:
            return self.by_val0.get(_key(v, 0)) or []
        return []

    def context(self, di: int, pos: int, span: int = 46) -> str:
        body = self.docs[di]["text"]
        s = body[max(0, pos - span): pos + span]
        # 引述里不要页标记与折行，读起来才像一句话
        s = _PAGE_MARK.sub(" ", s)
        s = re.sub(r"\s+", " ", s).strip()
        return ("…" + s if pos - span > 0 else s) + ("…" if pos + span < len(body) else "")


# ── 正文数字抽取 ──────────────────────────────────────────────────────
def scan(text: str) -> list[dict]:
    """扫出一段话里的数字，带单位、所在句子、在本段中的字符位置。

    位置（`at`）是给前端用的：页面点哪个字符，后端就得知道是哪一个数。
    两边用同一套规则扫，才能对上号。
    """
    text = text or ""
    out: list[dict] = []
    for m in _NUM.finditer(text):
        v = _to_float(m.group(0))
        if v is None:
            continue
        tail = text[m.end(): m.end() + 6]
        unit = ""
        for u in _UNITS:
            if tail.startswith(u):
                unit = u
                break
        if not unit and tail[:1] in ("年", "月", "日"):
            unit = "期次"
        elif not unit:
            for w in _META_TAIL:
                if tail.startswith(w):
                    unit = "meta"
                    break
        # 所在句子（只用于展示与相关性打分，不参与定位）
        a = max(text.rfind("。", 0, m.start()), text.rfind("；", 0, m.start())) + 1
        b = text.find("。", m.end())
        sent = text[a: (b + 1 if b > 0 else len(text))].strip()
        # 「约」在前：是估算，溯源时要说清「材料给的是约数还是精确值」
        approx = text[max(0, m.start() - 2): m.start()].strip().startswith("约")
        out.append({"raw": m.group(0), "num": v, "unit": _norm_unit(unit),
                    "at": m.start(), "sent": sent[:300], "approx": approx})
    return out


# 正文各段：(field, 给人看的段名, 取值函数)。顺序就是页面上的阅读顺序，
# 溯源清单按这个顺序排，人从第一条往下看正好跟着正文走。
def body_blocks(draft: dict) -> list[tuple[str, str, str]]:
    c = draft.get("commentary") or {}

    def g(k: str, f: str = "body") -> str:
        return ((c.get(k) or {}).get(f) or "")

    out: list[tuple[str, str, str]] = [
        ("basic", "基本情况", draft.get("basic_info") or ""),
        ("in-lead", "点评·行业加粗句", g("industry", "lead")),
        ("in", "点评第一段（行业）", g("industry")),
        ("ov-lead", "总述段加粗句", g("overview", "lead")),
        ("ov", "总述段", g("overview")),
    ]
    for i, s in enumerate(c.get("segments") or []):
        out.append(("seg", f"{i + 1}）{s.get('name') or '板块'}", s.get("body") or ""))
    out += [
        ("pf-lead", "盈利与现金流段加粗句", g("profit", "lead")),
        ("pf", "盈利与现金流段", g("profit")),
        ("as-lead", "资产段加粗句", g("asset", "lead")),
        ("as", "资产段", g("asset")),
        ("db-lead", "债务段加粗句", g("debt", "lead")),
        ("db", "债务段", g("debt")),
        ("mk-lead", "二级市场段加粗句", g("market", "lead")),
        ("mk", "二级市场定价", g("market")),
    ]
    for i, t in enumerate(c.get("special_topics") or []):
        out.append(("topic", f"专题段{i + 1}·{t.get('title') or ''}", t.get("body") or ""))
    for i, t in enumerate(draft.get("positive") or []):
        out.append(("pos", f"正面{i + 1}）", str(t or "")))
    for i, t in enumerate(draft.get("concern") or []):
        out.append(("con", f"关注{i + 1}）", str(t or "")))
    return out


# ── 算式：模型交底的优先，同段反推的兜底 ────────────────────────────────
_MODE_TEXT = {
    "ratio": ("占比", "÷{b} × 100%"),
    "growth": ("增速", "(−{b}) ÷ {b} × 100%"),
    "diff": ("差额", "−{b}"),
    "sum": ("合计", "+{b}"),
    "rest": ("结构调整", "(−{b}) ÷ {a} × 100%"),
    "times": ("倍数", "÷{b}"),
}


def _mode_expr(mode: str, a: float, b: float) -> str:
    """把一条反推出来的算式写成一句人能读的话。"""
    A, B = G._nf(a), G._nf(b)
    if mode == "ratio":
        return f"{A} ÷ {B} × 100%"
    if mode == "growth":
        return f"({A} − {B}) ÷ {B} × 100%"
    if mode == "rest":
        return f"({A} − {B}) ÷ {A} × 100%"
    if mode == "diff":
        return f"{A} − {B}"
    if mode == "sum":
        return f"{A} + {B}"
    return f"{A} ÷ {B}"


def _unit_kind(unit: str) -> str:
    if unit in ("%", "个百分点"):
        return "pct"
    if unit == "倍":
        return "times"
    if unit in ("亿元", "万元", "元"):
        return "money"
    return "plain"


def note_formulas(draft: dict) -> list[dict]:
    """把 data_notes 里交底的算式解析成可查的表：结果值 → 算式。

    模型交底的算式是「它自己说的算法」，比事后反推可信一个量级：
    反推只能告诉你「这两个数能算出它」，交底能告诉你「分子是短期债务、分母是全部债务」。
    """
    out: list[dict] = []
    for note in (draft.get("data_notes") or []):
        f = G._parse_formula(str(note))
        if not f:
            continue
        val = G._eval_formula(f["expr"], f["nums"])
        if val is None:
            continue
        is_pct = ("%" in f["expr"]) or ("％" in f["expr"]) or ("个百分点" in (f.get("raw") or ""))
        out.append({"name": f["name"], "expr": f["expr"], "raw": f["raw"],
                    "val": val, "operands": f["operands"], "pct": is_pct})
    return out


# ── 主流程 ────────────────────────────────────────────────────────────
def build(draft: dict, materials: list[dict] | None = None) -> dict:
    """给一份草稿里的每个数字定位来源。

    materials 传的是「这次真正进了提示词的摘录」（含 {title, path, text, pages}）。
    """
    idx = MatIndex()
    for m in (materials or []):
        if m.get("text"):
            idx.add(m.get("title") or "", m["text"], m.get("path") or "", m.get("pages") or 0)

    formulas = note_formulas(draft)
    rows: list[dict] = []
    # 同段反推的表按段缓存：一段只建一次，几十个数两两组合也就几千条
    tables: dict[str, dict] = {}
    pools: dict[str, list[float]] = {}

    for field, name, text in body_blocks(draft):
        if not text:
            continue
        toks = scan(text)
        if not toks:
            continue
        # 反推的操作数池只收「真正的量」：年份与篇幅混进去，两两组合会凑出
        # 一大堆假算式（2024 − 2023 之类），看着像复算成功，实际是噪声。
        pools[field] = [t["num"] for t in toks if t["unit"] not in ("期次", "meta")]
        for t in toks:
            row = _trace_one(t, field, name, idx, formulas, tables, pools)
            if row:
                rows.append(row)

    # 同一段里同一个数出现多次（「14.76%，较上年提高 1.23 个百分点」），
    # 各自独立溯源；编号按出现顺序，前端据此与页面上的字符位置对上。
    stats = {"total": 0, "source": 0, "calc": 0, "none": 0, "meta": 0}
    for r in rows:
        if r["kind"] == "meta":
            stats["meta"] += 1
            continue
        stats["total"] += 1
        stats[r["kind"]] = stats.get(r["kind"], 0) + 1
    done = stats["source"] + stats["calc"]
    stats["coverage"] = round(done / stats["total"] * 100, 1) if stats["total"] else 0.0
    return {"stats": stats, "rows": rows,
            "docs": [{"i": d["i"], "title": d["title"], "pages": d["pages"],
                      "path": d["path"]} for d in idx.docs]}


def _trace_one(t: dict, field: str, name: str, idx: MatIndex,
               formulas: list[dict], tables: dict, pools: dict) -> dict | None:
    """一个数字的溯源：材料原值 + 交底算式 + 同段反推，三样都查，能查到几样给几样。

    不是「命中一个就收工」：一个比率既可能是材料表格里抄来的，也可能是模型自己
    用两个数算的。两种都摆出来，人一眼就知道该信哪个；只报其中一种，等于替人
    做了一个它会做错的判断。
    """
    base = {"num": t["raw"] + ("" if t["unit"] in ("meta", "期次") else t["unit"]),
            "raw": t["raw"], "unit": t["unit"], "field": field, "blk": name,
            "at": t["at"], "sent": t["sent"], "approx": t["approx"]}
    if t["unit"] in ("期次", "meta"):
        base.update({"kind": "meta", "note": "期次或篇幅，不参与数据核对"})
        return base

    hits = _search(idx, t["num"], t["unit"], t["sent"])
    card: dict | None = None

    # ① 交底算式：模型自己写下的算法，比事后反推可信
    f = _match_formula(t["num"], t["unit"], formulas)
    if f:
        card = _calc_card(f, idx)

    # ② 同段反推：只在材料里找不到、也没交底算式时才做。
    #    材料已经给出原值时再反推一遍，多半能凑出「46.98 − 4.66 = 42.32」这种
    #    数值成立、意思不对的组合，摆出来只会让人怀疑那个本来正确的出处。
    if not card and not hits:
        pool = pools.get(field) or []
        if 2 < len(pool) <= 60:
            if field not in tables:
                tables[field] = G._formula_table(pool)
            card = _reverse(t["num"], t["unit"], tables[field], idx)
            if card:
                card["guess"] = True

    base["hits"] = hits[:3]
    base["calc"] = card
    if hits:
        base["kind"] = "source"
        top = hits[0]
        base.update({"doc": top["doc"], "doc_title": top["doc_title"],
                     "doc_path": top.get("doc_path") or "",
                     "page": top["page"], "quote": top["quote"]})
        if len(hits) > 1:
            base["note"] = f"同一数值在材料里出现 {len(hits)} 处，已按上下文挑最相关的一处"
    elif card:
        base["kind"] = "calc"
        base["note"] = ("没有交底算式，按同段数字反推，请确认是不是这两个数"
                        if card.get("guess") else "")
    elif field.startswith("mk"):
        # 二级市场的利差与 BP 变动来自中债估值曲线，材料里本来就没有——
        # 按「找不到出处」报出来是假警报，这一段的来源是行情接口。
        base["kind"] = "market"
        base["note"] = "来自中债估值曲线，与右侧利差图同源；材料里不会有这个数"
    else:
        base["kind"] = "none"
        base["note"] = (
            "这是两期百分比之差：材料摘录里没有对照期那个数，翻全文才核得到"
            if t["unit"] == "个百分点" else
            "材料摘录里没有这个值，也没有可复原的算式：可能是换算、扣减或笔误。"
            "点「翻全文」把整份 PDF 再找一遍。")
    return base


def _search(idx: MatIndex, num: float, unit: str, sent: str) -> list[dict]:
    """按值在材料里找，按上下文相关度排序。

    相关性只用来挑「最该给人看的那一处」：同一个 8.08 亿在合并表和母公司表里
    各出现一次，跟正文那句话词重合多的那个，多半就是它引的那张表。

    单位必须先过一道筛：正文说「3.21%」，材料里那个 3.21 是「3.21 亿元」，
    数值一模一样但完全不是一回事。金额与比率互斥，单位对上的再加分。
    """
    want = _unit_kind(unit)
    seen: set[tuple[int, int]] = set()
    out: list[dict] = []
    for v in cand_values(num, unit):
        for di, pos, page, mu in idx.lookup(v):
            if (di, pos) in seen:
                continue
            seen.add((di, pos))
            mk = _unit_kind(mu)
            if want == "money" and mk == "pct":
                continue
            if want == "pct" and mk == "money":
                continue
            if want == "times" and mk not in ("times", "plain"):
                continue
            quote = idx.context(di, pos)
            same = bool(mu) and mk == want
            out.append({"doc": di, "doc_title": idx.docs[di]["title"],
                        "doc_path": idx.docs[di]["path"], "page": page,
                        "quote": quote, "same_unit": same,
                        "score": _overlap(sent, quote) + (2.0 if same else 0.0)})
    out.sort(key=lambda x: (-x["score"], -int(x["same_unit"]), x["page"], x["doc"]))
    return out


def _overlap(a: str, b: str) -> float:
    """两个片段共有多少个二字组合（去掉数字，避免「都是 8.08」这种假相关）。"""
    def grams(s: str) -> set[str]:
        s = re.sub(r"[0-9,.\s%％+\-−×÷]", "", s or "")
        return {s[i:i + 2] for i in range(len(s) - 1)}
    ga, gb = grams(a), grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / min(len(ga), len(gb))


def _match_formula(num: float, unit: str, formulas: list[dict]) -> dict | None:
    """找一条算出来正好等于这个数的交底算式。百分比与金额分开认，免得
    把「14.76（亿元）」错配到「14.76%」那条算式上。"""
    want = _unit_kind(unit)
    for f in formulas:
        if G._close(abs(f["val"]), abs(num)):
            if f["pct"] and want not in ("pct", "plain"):
                continue
            if not f["pct"] and want == "pct":
                continue
            return f
    return None


def _calc_card(f: dict, idx: MatIndex) -> dict:
    """算式卡：算式本身 + 每个操作数各自的来源。"""
    ops: list[dict] = []
    for v in (f.get("operands") or []):
        hits = _search(idx, v, "", f.get("name") or "")
        one = {"v": v, "disp": G._nf(v)}
        if hits:
            one.update({"doc": hits[0]["doc"], "page": hits[0]["page"],
                        "quote": hits[0]["quote"], "doc_title": hits[0]["doc_title"],
                        "doc_path": hits[0].get("doc_path") or ""})
        else:
            one["miss"] = True
        ops.append(one)
    return {"name": f.get("name") or "", "expr": f.get("expr") or "",
            "disp": _pretty_expr(f.get("expr") or ""), "val": f.get("val"),
            "ops": ops, "confident": True}


def _pretty_expr(expr: str) -> str:
    """把 `a/b*100` 改写成 `a ÷ b × 100%`，与正文里的写法一致。"""
    s = (expr or "").replace("*", "×").replace("/", "÷")
    s = s.replace("100%", "100%")
    return re.sub(r"\s+", " ", s).strip()


def _reverse(num: float, unit: str, table: dict, idx: MatIndex) -> dict | None:
    """同段两个数能不能算出它。能就返回算式，带每个原数的来源。

    这里给的是「可能」，不是「就是」：反推只知道数值关系，不知道业务含义
    （算出来的 42.25% 到底是资产负债率还是短期债务占比，反推说不出来）。
    所以卡片上要写明这是反推。
    """
    want = _unit_kind(unit)
    for mode in G._MODES:
        got = table.get((mode, round(abs(num), 2)))
        if not got:
            # 差额可能为负（「较上年减少 40.76 亿元」与「−40.76」两种写法），
            # 原值带符号的组合也要试一次，否则下降类差额全都反推不出来。
            got = table.get((mode, round(num, 2)))
        if not got:
            continue
        a, b, _v = got
        kind = "pct" if mode in ("ratio", "growth", "rest") else (
            "times" if mode == "times" else "money")
        if want != "plain" and kind != want:
            continue
        ops = []
        for v in (a, b):
            hits = _search(idx, v, unit, "")
            one = {"v": v, "disp": G._nf(v)}
            if hits:
                one.update({"doc": hits[0]["doc"], "page": hits[0]["page"],
                            "quote": hits[0]["quote"], "doc_title": hits[0]["doc_title"],
                            "doc_path": hits[0].get("doc_path") or ""})
            else:
                one["miss"] = True
            ops.append(one)
        label = _MODE_TEXT.get(mode, ("计算", ""))[0]
        return {"name": f"按同段数字反推（{label}）", "expr": _mode_expr(mode, a, b),
                "disp": _mode_expr(mode, a, b), "val": num, "ops": ops,
                "confident": False}
    return None


# ── 翻全文：摘录没覆盖到的页 ──────────────────────────────────────────
def deep_search(pdf_path: str, num: str, unit: str = "", limit: int = 12,
                near_limit: int = 3) -> dict:
    """在 PDF 全文里找这个数，返回页面与上下文。

    为什么单独做一个：进提示词的是摘录（几十页里挑十来页），模型没看到全文。
    摘录里查不到不等于材料里没有——这个功能就是回答「到底是没写，还是没看到」。

    找不到时给「最接近的几个」：7.60 亿在材料里可能写成 7.61 或 75,980.00 万，
    差一点点就搜不到。只回一句「没有」，人还是得自己去翻；给出最接近的那几个，
    一眼就能看出是四舍五入、口径不同，还是真的没有这个数。
    """
    from pdf_text import extract_pages      # 局部导入：只有这个功能需要 pymupdf

    v = _to_float(num)
    if v is None:
        return {"pages": [], "near": []}
    # 只按两位小数精确比：f"{7.6:.0f}" 会变成 8，把材料里所有的 8 都当天命中，
    # 结果就是「找到了，在第 10 页」，引文却是另一个数——比说没找到更误事。
    keys: set[str] = set()
    for x in cand_values(v, unit):
        keys.add(_key(x, 2))
        if abs(x) >= 1000:
            keys.add(_key(x, 0))
    out: list[dict] = []
    near: list[tuple[float, int, str]] = []
    for i, page in enumerate(extract_pages(pdf_path)):
        txt = page or ""
        for m in _NUM.finditer(txt):
            got = _to_float(m.group(0)) or 0
            if _key(got, 2) in keys or (abs(got) >= 1000 and _key(got, 0) in keys):
                s = txt[max(0, m.start() - 50): m.end() + 50]
                out.append({"page": i + 1, "quote": re.sub(r"\s+", " ", s).strip()})
                break
            if v and got:
                rel = abs(abs(got) - abs(v)) / abs(v)
                if rel <= 0.03:
                    s = txt[max(0, m.start() - 40): m.end() + 40]
                    near.append((round(rel, 4), i + 1,
                                 re.sub(r"\s+", " ", s).strip()))
        if len(out) >= limit:
            break
    near.sort(key=lambda x: x[0])
    seen: set[int] = set()
    near_out: list[dict] = []
    for rel, pg, q in near:
        if pg in seen:
            continue
        seen.add(pg)
        near_out.append({"page": pg, "quote": q, "rel": rel})
        if len(near_out) >= near_limit:
            break
    return {"pages": out[:limit], "near": near_out if not out else []}


def page_image(pdf_path: str, page: int, dpi: int = 150) -> bytes:
    """把某一页渲成图片，让「回到源文件」是真的看得见那一页，而不是只给个页码。"""
    try:
        import pymupdf
    except ImportError:                      # 老版本包名
        import fitz as pymupdf             # type: ignore
    doc = pymupdf.open(pdf_path)
    try:
        if not (1 <= page <= doc.page_count):
            return b""
        pix = doc[page - 1].get_pixmap(dpi=dpi)
        return pix.tobytes("jpeg", jpg_quality=72)
    finally:
        doc.close()
