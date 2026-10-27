"""分析附件：发债情况、关键财务信息。

这两份是写报告前的底料——存续债券摊在一页上、三期主要财务数据摊在另一页上，
核对过再动笔。原先它们散在几十页 PDF 里，要翻到第 12 页才看得到「一年内到期
多少」，所以做成两个纯文本：侧栏点开就能看，打包与发信时也能勾。

取材原则：
- 债券表按「图表标题 + 一行一个单元格」的版面结构还原成表，不做语义猜测；
  还原不了就不出表，只出摘句，绝不拼一张看起来像表的错表。
- 摘句只取带金额或百分比的句子：纯定性的句子（「公司债券规模适中」）不进——
  那是判断，判断由正文去写，附件只负责把原话摆出来。
- 摘不到就明说摘不到，不拿别的数字凑。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

# ── 摘句话题 ────────────────────────────────────────────────────────────
# 名字 → 关键词。命中即归到该话题，跨话题只归第一个，
# 免得同一句话在「存续债券」和「到期回售」里各出现一次。
DEBT_TOPICS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("评级", ("主体信用等级", "信用等级为", "主体评级", "评级展望", "级别为",
            "上调", "下调")),
    ("债券余额", ("债券余额", "存续债券", "存续期", "余额合计", "发行金额",
              "债券品种", "中期票据", "公司债券", "永续债", "可续期")),
    ("募集资金", ("募集资金", "募集", "指定用途", "注册额度", "核准")),
    ("到期与回售", ("一年内到期", "待偿", "到期", "回售", "赎回", "兑付",
               "集中偿付", "偿还安排")),
)

FIN_TOPICS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("规模与结构", ("资产总额", "总资产", "负债总额", "所有者权益", "净资产",
               "资产负债率", "流动资产", "非流动资产")),
    ("经营与盈利", ("营业总收入", "营业收入", "净利润", "利润总额", "毛利率",
               "期间费用", "投资收益", "其他收益")),
    ("现金流", ("经营活动现金流量净额", "经营活动现金流", "现金及现金等价物",
             "投资活动", "筹资活动")),
    ("债务与偿债", ("全部债务", "短期债务", "长期债务", "有息债务", "短期借款",
               "长期借款", "应付债券", "EBITDA", "利息保障", "流动比率",
               "速动比率", "总资本化比率", "授信")),
)

MAX_PER_TOPIC = 8

# 金额或百分比：只有「2025 年」这种年份不算——那种句子大多是纯定性判断
_MONEY = re.compile(r"\d+(?:\.\d+)?\s*(?:亿元|万元|元|%|％|倍|个百分点)"
                    r"|\d{1,3}(?:,\d{3})+(?:\.\d+)?")
# 页眉页脚：PDF 每页开头都有「[第N页]跟踪评级报告|3」，混进正文会把句子切断
_HEADER = re.compile(r"\[第\d+页\]|(?:跟踪评级报告|募集说明书|信用评级报告)\s*[|｜]\s*\d+")


def _s(v: Any) -> str:
    return "" if v is None else str(v).strip()


def _clean(text: str) -> str:
    return _HEADER.sub("", text or "")


def _flat(text: str) -> str:
    """PDF 抽出来是按版面换行的，先把空白全部去掉再切句。"""
    return re.sub(r"\s+", "", _clean(text))


def _sentences(text: str) -> list[str]:
    out: list[str] = []
    for s in re.split(r"[。；!?]", _flat(text)):
        s = s.strip("，、：: ")
        if 14 <= len(s) <= 300:
            out.append(s)
    return out


def _money_sentence(s: str) -> bool:
    return bool(_MONEY.search(s))


def _trim(s: str, limit: int = 150) -> str:
    """太长就在逗号处截断，别把半句话留在纸上。"""
    if len(s) <= limit:
        return s
    cut = max(s.rfind(c, 0, limit) for c in "，、；")
    return (s[:cut] if cut > 60 else s[:limit]).rstrip("，、；") + "……"


def _pick(materials: list[dict], topics, limit: int = MAX_PER_TOPIC) -> list[tuple[str, list[str]]]:
    """按话题摘句，返回 [(话题, [句子…])]，每句括注出处材料名。"""
    buckets: dict[str, list[str]] = {name: [] for name, _ in topics}
    seen: set[str] = set()
    for m in materials or []:
        if not isinstance(m, dict):
            continue
        title = re.sub(r"(_[0-9a-f]{12})+$", "", _s(m.get("title") or m.get("name")))
        for sent in _sentences(m.get("text") or ""):
            if not _money_sentence(sent):
                continue
            key = sent[:22]
            if key in seen:
                continue
            for name, words in topics:
                if any(w in sent for w in words):
                    if len(buckets[name]) < limit:
                        t = _trim(sent)
                        buckets[name].append(f"{t}。（{title}）" if title else f"{t}。")
                        seen.add(key)
                    break
    return [(name, buckets[name]) for name, _ in topics]


# ── 债券表还原 ──────────────────────────────────────────────────────────
# 评级报告与募集说明书里的表，PDF 抽出来几乎都是「一行一个单元格」：
#   图表1 • ……存续债券概况
#   债券简称 / 发行金额（亿元） / 债券余额（亿元） / 起息日 / 期限（年）
#   19 建材09 / 23.00 / 23.00 / 2019/08/05 / 10
# 所以能按「表头行数」分组还原。表头行数 = 出现第一个含数字的单元格之前的行数。
_CHART = re.compile(r"^图表\s*\d+\s*[•·]?\s*(.+)$", re.M)


def _charts(text: str) -> list[tuple[str, str]]:
    hits = list(_CHART.finditer(_clean(text)))
    out: list[tuple[str, str]] = []
    for i, h in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(hits and text)
        out.append((h.group(1).strip(), _clean(text)[h.end():end]))
    return out


def _cells(block: str) -> list[str]:
    lines = [l.strip() for l in block.split("\n")]
    lines = [l for l in lines if l]
    for i, l in enumerate(lines):
        if l.startswith(("注：", "注:", "资料来源", "数据来源")):
            return lines[:i]
    return lines


def _parse_table(lines: list[str]) -> tuple[list[str], list[list[str]]] | None:
    n = 0
    for l in lines:
        if re.search(r"\d", l) or len(l) > 16:
            break
        n += 1
    if not (2 <= n <= 8):
        return None
    body = lines[n:]
    if len(body) < n:
        return None
    rows = [body[i:i + n] for i in range(0, len(body) - n + 1, n)]
    return lines[:n], rows


def _bond_table(materials: list[dict]) -> tuple[str, str, list[str], list[list[str]]] | None:
    """找回存续债券那张表：(材料名, 标题, 表头, 行)。"""
    for m in materials or []:
        if not isinstance(m, dict):
            continue
        name = re.sub(r"(_[0-9a-f]{12})+$", "", _s(m.get("title") or m.get("name")))
        for title, block in _charts(m.get("text") or ""):
            if "债" not in title or not any(w in title for w in ("存续", "债券概况", "债券情况", "发行")):
                continue
            parsed = _parse_table(_cells(block))
            if not parsed:
                continue
            head, rows = parsed
            # 至少三行、且第一列看起来像债券简称，才认它是一张真表
            if len(rows) < 3 or not all(len(r) == len(head) for r in rows):
                continue
            if not any(re.search(r"[A-Za-z\u4e00-\u9fa5]", r[0] or "") for r in rows):
                continue
            return name, title, head, rows
    return None


def _wrap(prefix: str, text: str, width: int = 78) -> list[str]:
    """按宽度折行，续行缩进对齐——纯文本没有排版，长行读起来费劲。"""
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


class _Doc:
    def __init__(self, title: str, draft: dict, when: str):
        self.lines = [title, "=" * 62]
        usage = (draft or {}).get("usage") or {}
        meta = [f"整理时间：{when}"]
        if usage.get("model"):
            meta.append(f"模型：{usage['model']}")
        self.lines.append("　·　".join(meta))
        self.lines.append("")

    def h(self, text: str):
        self.lines.append(text)
        self.lines.append("-" * 62)

    def p(self, text: str = ""):
        self.lines.append(text)

    def text(self) -> str:
        return "\n".join(self.lines).rstrip() + "\n"


def _materials_head(materials: list[dict]) -> str:
    names = [re.sub(r"(_[0-9a-f]{12})+$", "", _s(m.get("title") or m.get("name")))
             for m in (materials or []) if isinstance(m, dict)]
    names = [n for n in names if n]
    return "、".join(names[:5]) + ("……" if len(names) > 5 else "")


def debt_txt(draft: dict, when: str, materials: list[dict] | None = None) -> str:
    """发债情况：存续债券一览（还原成表）、要点摘录。"""
    d = _Doc(f"{_s((draft or {}).get('company_full_name')) or '未命名主体'} · 发债情况",
             draft or {}, when)
    d.h("一、说明")
    d.p("第二节的债券表按材料版面的表格结构还原（制表符分隔，可直接粘进 Excel）；")
    d.p("第三节每句摘自材料原文，只做了去换行与断句，未改写。括注是出处材料。")
    if _materials_head(materials):
        d.p(f"材料　　{_materials_head(materials)}")
    d.p()

    tb = _bond_table(materials or [])
    d.h("二、存续债券一览")
    if tb:
        name, title, head, rows = tb
        d.p(f"原表标题：{title}")
        if name:
            d.p(f"出处：{name}")
        d.p()
        d.p("\t".join(head))
        for r in rows:
            d.p("\t".join(r))
        d.p()
        # 债券余额列若整列都能读成数，给一个加总；标清是「按上表加总」，
        # 不与原文的「余额合计」混为一谈——两者口径可能不同。
        for i, h in enumerate(head):
            if "余额" in h:
                vals = []
                for r in rows:
                    v = re.sub(r"[^\d.]", "", (r[i] or ""))
                    if v:
                        vals.append(float(v))
                if vals and len(vals) == len(rows):
                    d.p(f"按上表第「{h.strip()}」列加总：{sum(vals):,.2f}"
                        f"（共 {len(rows)} 支；口径以上表为准）")
                break
        d.p()
    else:
        d.p("没有识别出存续债券表（材料里可能没有这张表，或版式不同）。")
        d.p("下面第三节仍有债券相关原句可参考。")
        d.p()

    picked = _pick(materials or [], DEBT_TOPICS)
    d.h("三、要点摘录")
    total = 0
    for name, rows in picked:
        if not rows:
            continue
        total += len(rows)
        d.p(f"【{name}】")
        for r in rows:
            for ln in _wrap("· ", r):
                d.p(ln)
        d.p()
    if not total:
        d.p("本次材料里没有抽出带金额的债券表述，多半是材料里没有债券章节。")
        d.p("补一份募集说明书或跟踪评级报告的债券章节后再试。")
    return d.text()


def key_fin_txt(draft: dict, when: str, materials: list[dict] | None = None) -> str:
    """关键财务信息：三期主要数据与指标（表）+ 收入构成 + 材料原句。"""
    d = _Doc(f"{_s((draft or {}).get('company_full_name')) or '未命名主体'} · 关键财务信息",
             draft or {}, when)
    c = (draft or {}).get("commentary") or {}
    fin = c.get("financial_table") or {}

    d.h("一、主要财务数据与指标")
    periods = [_s(x) for x in (fin.get("periods") or [])]
    rows = fin.get("rows") or []
    if periods and rows:
        d.p(_s(fin.get("caption")) or "（合并口径）")
        d.p(_s(fin.get("unit_line")) or "单位：亿元")
        # 制表位分隔：纯文本里能对齐，粘进 Excel 也自动分列
        d.p("指标\t" + "\t".join(periods))
        for r in rows:
            cells = [_s(x) for x in r]
            if not cells:
                continue
            d.p("\t".join([cells[0]] + cells[1:1 + len(periods)]))
        d.p()
        d.p("说明：期次由新到旧排列，最近一期在最左；「—」表示该项材料未给出。")
    else:
        d.p("这次没有生成财务指标表（材料里没找到可用数据）。")
    d.p()

    seg = c.get("segment_table") or {}
    if len(seg.get("rows") or []) > 1:
        d.h("二、收入构成")
        d.p(_s(seg.get("unit_line")) or "单位：亿元")
        header = [_s(x) for x in (seg.get("header") or [])]
        if header:
            d.p("\t".join(header))
        for r in seg["rows"]:
            d.p("\t".join(_s(x) for x in r))
        d.p()

    picked = _pick(materials or [], FIN_TOPICS)
    d.h("三、材料原文摘录")
    d.p("只摘带金额或百分比的句子，按话题分组；同一句只出现一次。")
    d.p()
    for name, rows_ in picked:
        if not rows_:
            continue
        d.p(f"【{name}】")
        for r in rows_:
            for ln in _wrap("· ", r):
                d.p(ln)
        d.p()
    if not any(rows_ for _n, rows_ in picked):
        d.p("本次材料里没有抽出带金额的财务表述。")
    return d.text()


# key 用于前后端传参，name 是落盘文件名，desc 给人看一行
SPECS = (
    ("debt", "发债情况", "存续债券表与评级、余额、到期摘录", debt_txt),
    ("fin", "关键财务信息", "三期主要指标表、收入构成与原文摘录", key_fin_txt),
)


def build(draft: dict, when: str | None = None, materials: list | None = None) -> list[dict]:
    """生成两份分析附件：[{key, name, desc, chars, text}]。"""
    when = when or datetime.now().strftime("%Y-%m-%d %H:%M")
    company = _s((draft or {}).get("company_full_name")) or "未命名主体"
    safe = "".join(ch for ch in company if ch not in '\\/:*?"<>|').strip() or "未命名主体"
    out: list[dict] = []
    for key, title, desc, fn in SPECS:
        text = fn(draft or {}, when, materials)
        out.append({"key": key, "name": f"{safe}_{title}.txt", "dir": "分析附件",
                    "desc": desc, "chars": len(text), "text": text})
    return out


def one(draft: dict, key: str, when: str | None = None,
        materials: list | None = None) -> dict | None:
    for f in build(draft, when, materials):
        if f["key"] == key:
            return f
    return None


def meta_list(draft: dict, materials: list | None = None) -> list[dict]:
    """只要清单，不要全文——侧栏与两个勾选面板都用它。"""
    return [{k: f[k] for k in ("key", "name", "desc", "chars")}
            for f in build(draft, None, materials)]
