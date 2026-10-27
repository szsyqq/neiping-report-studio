"""分析过程附件：把这次生成的分析产物整理成几个纯文本文件。

为什么要单独出 txt：正文 Word 是给分管看的成品，分析过程（哪些数没对上、
每个自算值怎么算的、每句话的出处、被刷掉的候选观点）原本只活在这一屏里，
关掉页面就没了。审稿的人要复核、归档的人要留痕，都需要一份能直接读的文本。

只做整理，不做分析：这里出现的每个字都来自 draft，不新增判断。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any


def _s(v: Any) -> str:
    return "" if v is None else str(v).strip()


def _wrap(prefix: str, text: str, width: int = 76) -> list[str]:
    """按宽度折行，续行缩进对齐——纯文本没有排版，太长的行读起来费劲。"""
    t = _s(text)
    if not t:
        return []
    lines: list[str] = []
    rest = t
    first = True
    while rest:
        head = prefix if first else " " * len(prefix)
        take = width - len(prefix)
        lines.append(head + rest[:take])
        rest = rest[take:]
        first = False
    return lines


class _Doc:
    def __init__(self, title: str, draft: dict, when: str, materials: list | None = None):
        self.lines = [title, "=" * 60]
        usage = draft.get("usage") or {}
        meta = [f"生成时间：{when}"]
        if usage.get("model"):
            meta.append(f"模型：{usage['model']}")
        if usage.get("total_tokens"):
            meta.append(f"本次用量：{usage['total_tokens']:,} token")
        self.lines.append("　·　".join(meta))
        self.lines.append("")

    def h(self, text: str):
        self.lines.append(text)
        self.lines.append("-" * 60)

    def p(self, text: str = ""):
        self.lines.append(text)

    def text(self) -> str:
        return "\n".join(self.lines).rstrip() + "\n"


def _materials_line(rows: list) -> str:
    names = [(_s(m.get("title")) or _s(m.get("name"))) for m in (rows or []) if isinstance(m, dict)]
    # 缓存文件名带 _<12位哈希> 后缀，有时还会重复两遍；这份是给人读的，去掉
    names = [re.sub(r"(_[0-9a-f]{12})+$", "", n) for n in names if n]
    return "、".join(names[:6]) + ("……" if len(names) > 6 else "")


def checks_txt(draft: dict, when: str, materials: list | None = None) -> str:
    """核对结果：待核对逐条 + 机器已复算的算式。"""
    d = _Doc(f"{_s(draft.get('company_full_name')) or '未命名主体'} · 核对结果", draft, when, materials)
    v = draft.get("validation") or {}
    items = v.get("issue_items") or []
    rows = [r for it in items for r in (it.get("d") or [])]
    auto = v.get("auto_checks") or []
    d.h("一、概览")
    d.p(f"正文　　{v.get('body_chars') or 0} 字")
    d.p(f"待核对　{len(items)} 组 / {len(rows)} 条（人工逐条确认后勾选）")
    d.p(f"已复算　{v.get('auto_count') or 0} 项（算式见本文件第三节）")
    if _materials_line(materials):
        d.p(f"材料　　{_materials_line(materials)}")
    d.p()

    d.h("二、待核对事项")
    if not items:
        d.p("没有待核对的事项。")
        d.p()
    for i, it in enumerate(items, 1):
        d.p(f"{i}. {_s(it.get('t')) or '未命名'}")
        for r in (it.get("d") or []):
            where = _s(r.get("where"))
            num = _s(r.get("num"))
            note = _s(r.get("note"))
            full = _s(r.get("full"))
            if where or num:
                # num 在不同类型里含义不同：数字类是被核的数，措辞类是命中的词。
                # 一律拼在「位置」后面会读成「位置 资产段 300.00亿元」，看着像地址。
                d.p(f"   位置　{where}" if not num else f"   位置　{where}　｜　{num}")
            for ln in _wrap("   说明　", note):
                d.p(ln)
            for ln in _wrap("   原文　", full, 74):
                d.p(ln)
        d.p()
    # 有些老记录只有自然语言的 issues，没有结构化条目，别让它们消失
    if not items and (v.get("issues") or []):
        for i, s in enumerate(v["issues"], 1):
            for n, ln in enumerate(_wrap(f"{i}. ", s, 76)):
                d.p(ln)
        d.p()

    d.h("三、机器已复算的算式")
    if auto:
        for ln in auto:
            d.p("· " + _s(ln).lstrip("·• "))
    else:
        d.p("本次没有可自动复算的项。")
    return d.text()


def notes_txt(draft: dict, when: str, materials: list | None = None) -> str:
    """自算算式：模型交底的口径。"""
    d = _Doc(f"{_s(draft.get('company_full_name')) or '未命名主体'} · 自算算式", draft, when, materials)
    notes = [_s(x) for x in (draft.get("data_notes") or []) if _s(x)]
    d.h("模型交底的算式与口径")
    if not notes:
        d.p("本次没有交底算式。")
    for i, note in enumerate(notes, 1):
        for n, ln in enumerate(_wrap(f"{i}. ", note, 76)):
            d.p(ln)
    d.p()
    d.p("说明：正文里凡不是材料直接给出、而是算出来的数（占比、增速、差额、倍数），")
    d.p("　　　按上述算式复算；算得上的不再进人工核对。")
    return d.text()


def sources_txt(draft: dict, when: str, materials: list | None = None) -> str:
    """来源对照：每句关键判断落在哪份材料的哪一处。"""
    d = _Doc(f"{_s(draft.get('company_full_name')) or '未命名主体'} · 来源对照", draft, when, materials)
    rows = [x for x in (draft.get("sources") or []) if isinstance(x, dict)]
    d.h("判断与出处")
    if not rows:
        d.p("本次没有登记来源对照。")
    for i, x in enumerate(rows, 1):
        claim = _s(x.get("claim"))
        where = _s(x.get("where"))
        d.p(f"{i}. {claim}")
        for ln in _wrap("   出处　", where, 74):
            d.p(ln)
    return d.text()


def views_txt(draft: dict, when: str, materials: list | None = None) -> str:
    """观点与建议：定稿摘要、候选池、可补充段落。"""
    d = _Doc(f"{_s(draft.get('company_full_name')) or '未命名主体'} · 观点与补充建议", draft, when, materials)
    d.h("一、定稿摘要")
    for key, label in (("positive", "正面"), ("concern", "关注")):
        items = [_s(x) for x in (draft.get(key) or []) if _s(x)]
        d.p(f"{label}（{len(items)} 条）")
        for i, t in enumerate(items, 1):
            for n, ln in enumerate(_wrap(f"  {i}）", t, 76)):
                d.p(ln)
        d.p()
    d.h("二、候选观点（够格但未进定稿）")
    any_pool = False
    for key, label in (("positive_pool", "正面候选"), ("concern_pool", "关注候选")):
        items = [_s(x) for x in (draft.get(key) or []) if _s(x)]
        if not items:
            continue
        any_pool = True
        d.p(f"{label}（{len(items)} 条）")
        for i, t in enumerate(items, 1):
            for n, ln in enumerate(_wrap(f"  {i}）", t, 76)):
                d.p(ln)
        d.p()
    if not any_pool:
        d.p("没有候选条目。")
        d.p()

    d.h("三、可补充段落")
    adv = [a for a in (draft.get("add_on_sections") or []) if isinstance(a, dict)]
    if not adv:
        d.p("没有建议补充的段落。")
    for i, a in enumerate(adv, 1):
        d.p(f"{i}. {_s(a.get('title'))}　（建议位置：{_s(a.get('where')) or '债务段之后'}）")
        if _s(a.get("auto")):
            d.p("   来源　材料原文摘录，需按报告语体重写")
        for ln in _wrap("   依据　", _s(a.get("why")), 74):
            d.p(ln)
        for ln in _wrap("   判断　", _s(a.get("lead")), 74):
            d.p(ln)
        for ln in _wrap("   正文　", _s(a.get("body")), 74):
            d.p(ln)
        d.p()
    return d.text()


def body_txt(draft: dict, when: str, materials: list | None = None) -> str:
    """正文全文：纯文本，便于比对与粘贴。"""
    d = _Doc(f"{_s(draft.get('company_full_name')) or '未命名主体'} · 正文全文", draft, when, materials)
    d.h("1、基本情况")
    d.p(_s(draft.get("basic_info")))
    d.p()
    c = draft.get("commentary") or {}
    d.h("2、点评")
    ov = c.get("overview") or {}
    if _s(ov.get("lead")):
        d.p("【" + _s(ov["lead"]) + "】")
    d.p(_s(ov.get("body")))
    d.p()
    seg_tbl = c.get("segment_table") or {}
    if seg_tbl.get("rows"):
        d.p(_s(seg_tbl.get("unit_line")) or "单位：亿元")
        d.p("\t".join(_s(x) for x in (seg_tbl.get("header") or [])))
        for r in seg_tbl.get("rows") or []:
            d.p("\t".join(_s(x) for x in r))
        d.p()
    for i, s in enumerate(c.get("segments") or [], 1):
        d.p(f"{i}）{_s(s.get('name'))}业务方面，{_s(s.get('body'))}")
        d.p()
    asr = c.get("asset") or {}
    if _s(asr.get("lead")):
        d.p("【" + _s(asr["lead"]) + "】")
    d.p(_s(asr.get("body")))
    d.p()
    fin = c.get("financial_table") or {}
    if fin.get("rows"):
        d.p(_s(fin.get("caption")) or "公司主要财务数据和财务指标（合并口径）")
        d.p(_s(fin.get("unit_line")) or "单位：亿元")
        d.p("\t".join(_s(x) for x in (fin.get("periods") or [])))
        for r in fin.get("rows") or []:
            d.p("\t".join(_s(x) for x in r))
        d.p()
    dbr = c.get("debt") or {}
    if _s(dbr.get("lead")):
        d.p("【" + _s(dbr["lead"]) + "】")
    d.p(_s(dbr.get("body")))
    for t in (c.get("special_topics") or []):
        d.p()
        if _s(t.get("lead")):
            d.p("【" + _s(t["lead"]) + "】")
        d.p(_s(t.get("body")))
    return d.text()


# key 用于前后端传参，name 是落盘文件名，desc 给人看一行
SPECS = (
    ("checks", "核对结果", "待核对逐条与机器已复算的算式", checks_txt),
    ("notes", "自算算式", "每个算出值是怎么算的", notes_txt),
    ("sources", "来源对照", "关键判断落在材料的哪一处", sources_txt),
    ("views", "观点与建议", "定稿摘要、候选观点、可补充段落", views_txt),
    ("body", "正文全文", "纯文本正文，便于比对与粘贴", body_txt),
)


def build(draft: dict, when: str | None = None, materials: list | None = None) -> list[dict]:
    """生成全部分析附件：[{key, name, desc, chars, text}]。"""
    when = when or datetime.now().strftime("%Y-%m-%d %H:%M")
    company = _s((draft or {}).get("company_full_name")) or "未命名主体"
    safe = "".join(c for c in company if c not in '\\/:*?"<>|').strip() or "未命名主体"
    out: list[dict] = []
    for i, (key, title, desc, fn) in enumerate(SPECS, 1):
        text = fn(draft or {}, when, materials)
        out.append({"key": key, "name": f"{safe}_分析{i}_{title}.txt", "dir": "分析附件",
                    "desc": desc, "chars": len(text), "text": text})
    return out


def one(draft: dict, key: str, when: str | None = None, materials: list | None = None) -> dict | None:
    for f in build(draft, when, materials):
        if f["key"] == key:
            return f
    return None


def meta_list(draft: dict, materials: list | None = None) -> list[dict]:
    """只要清单，不要全文——面板与打包勾选都用它。"""
    return [{k: f[k] for k in ("key", "name", "desc", "chars")} for f in build(draft, None, materials)]
