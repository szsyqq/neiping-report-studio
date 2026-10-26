#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
评级报告修订执行器：将 Action List 应用到旧报告 docx，
生成带 Word 原生修订标记（track changes，w:ins / w:del）的修订版 docx。

来源：`/Users/panyp/WorkBuddy/评级更新流程/scripts/apply_actions.py`。
本文件是它的副本，双方逻辑一致；改动请在那边改完再复制过来，
这样命令行流程与页面流程不会各走一套。只依赖 python-docx 与标准库，
所以云端部署（只有本项目目录）也能直接跑。

用法:
    python apply_actions.py --original 旧报告.docx --actions actions.json \
        [--output 修订版.docx] [--author "WorkBuddy"] [--log execution_log.json]

Action List 格式（来自分析阶段）:
    {
      "actions": [
        {
          "action_id": "A001",
          "action": "REPLACE|INSERT|DELETE|REVIEW",
          "location": "章节位置",
          "original_text": "旧报告原文（REPLACE/DELETE 必须精确匹配；INSERT 用于定位锚点）",
          "new_text": "新内容（REPLACE/INSERT 必填；DELETE/REVIEW 可空）",
          "reason": "理由",
          "evidence": "依据",
          "source": "来源"
        }
      ]
    }

行为规则（对齐分析提示词2）:
- 只执行 REPLACE / INSERT / DELETE；REVIEW 不执行。
- original_text 优先做「整段精确匹配」，其次做「段内子串匹配」。
- 找不到准确定位 → 保留原文，记入 failed_actions。
- 表格内段落同样处理。
- 输出修订版 docx + execution_log JSON。
"""
import argparse
import copy
import json
import re
import sys
import difflib
from datetime import datetime, timezone
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn


# ---------------------------------------------------------------- 修订构造 ---

class Revision:
    """管理修订 id 与作者信息的上下文。"""

    def __init__(self, author: str):
        self.author = author
        self._id = 0
        self.date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def next_id(self) -> int:
        self._id += 1
        return self._id


def _attr_map(rev: Revision, rev_type: str) -> dict:
    return {
        qn("w:id"): str(rev.next_id()),
        qn("w:author"): rev.author,
        qn("w:date"): rev.date,
    }


def _copy_rpr(src_r) -> object | None:
    """复制参考 run 的 rPr（无则返回 None）。"""
    rpr = src_r.find(qn("w:rPr"))
    if rpr is None:
        return None
    return copy.deepcopy(rpr)


def make_del_element(text: str, src_r, rev: Revision):
    """创建 <w:del><w:r><w:rPr/><w:delText>..</w:delText></w:r></w:del>"""
    del_el = src_r.makeelement(qn("w:del"), _attr_map(rev, "del"))
    r = src_r.makeelement(qn("w:r"), {})
    rpr = _copy_rpr(src_r)
    if rpr is not None:
        r.append(rpr)
    del_text = src_r.makeelement(qn("w:delText"), {})
    del_text.text = text
    r.append(del_text)
    del_el.append(r)
    return del_el


def make_del_from_run(src_r, rev: Revision):
    """将整个 run 转为删除标记：deepcopy run，w:t -> w:delText，外层包 w:del。"""
    r = copy.deepcopy(src_r)
    for t in r.findall(qn("w:t")):
        t.tag = qn("w:delText")
    del_el = src_r.makeelement(qn("w:del"), _attr_map(rev, "del"))
    del_el.append(r)
    return del_el


def make_ins_run(text: str, ref_r, rev: Revision):
    """创建 <w:ins><w:r><w:rPr/><w:t>..</w:t></w:r></w:ins>，参考 ref_r 的 rPr。"""
    ins_el = ref_r.makeelement(qn("w:ins"), _attr_map(rev, "ins"))
    r = ref_r.makeelement(qn("w:r"), {})
    rpr = _copy_rpr(ref_r)
    if rpr is not None:
        r.append(rpr)
    t = ref_r.makeelement(qn("w:t"), {qn("xml:space"): "preserve"})
    t.text = text
    r.append(t)
    ins_el.append(r)
    return ins_el


def clone_run_with_text(src_r, text: str):
    """deepcopy run 并设置纯文本（保留 rPr，清空其它子元素）。"""
    r = copy.deepcopy(src_r)
    for child in list(r):
        if child.tag != qn("w:rPr"):
            r.remove(child)
    t = src_r.makeelement(qn("w:t"), {qn("xml:space"): "preserve"})
    t.text = text
    r.append(t)
    return r


def set_run_text(src_r, text: str):
    """就地清空 run 并设置纯文本。"""
    for child in list(src_r):
        if child.tag != qn("w:rPr"):
            src_r.remove(child)
    t = src_r.makeelement(qn("w:t"), {qn("xml:space"): "preserve"})
    t.text = text
    src_r.append(t)


def run_text(src_r) -> str:
    return "".join(t.text or "" for t in src_r.findall(qn("w:t")))


def para_plain_text(p) -> str:
    return "".join(run_text(r) for r in p.r_lst)


# ---------------------------------------------------------------- 定位与执行 ---

def iter_paragraphs(doc):
    """按文档顺序遍历所有 w:p（含表格内段落）。"""
    for p in doc.element.body.iter(qn("w:p")):
        yield p


def locate_paragraph(doc, needle: str):
    """
    定位原文。
    返回 (p, start, end, mode)：
      mode="full"   段落整段文本 == needle
      mode="sub"    段内子串匹配，start/end 为偏移
    找不到返回 None。
    """
    if not needle:
        return None
    needle = needle.strip()
    for p in iter_paragraphs(doc):
        text = para_plain_text(p)
        if text.strip() == needle:
            return (p, 0, len(text), "full")
    # 二次：子串匹配（忽略首尾空白差异）
    for p in iter_paragraphs(doc):
        text = para_plain_text(p)
        idx = text.find(needle)
        if idx != -1:
            return (p, idx, idx + len(needle), "sub")
    return None


def mark_delete_range(p, start: int, end: int, rev: Revision):
    """
    将段落 p 中 [start,end) 的文本标记为删除。
    返回 (ok, last_del, last_anchor)：
      ok         是否成功
      last_del   最后一个删除标记节点（REPLACE 的 ins 应插在其后）
      last_anchor 最后被处理的节点（备用）
    """
    runs = list(p.r_lst)
    if not runs:
        return (False, None, None)
    texts = [run_text(r) for r in runs]
    offsets = []
    cur = 0
    for t in texts:
        offsets.append(cur)
        cur += len(t)
    if end > cur:
        return (False, None, None)

    last_del = None
    last_anchor = None
    for i, r in enumerate(runs):
        rs, re = offsets[i], offsets[i] + len(texts[i])
        if re <= start or rs >= end:
            continue
        a = max(0, start - rs)
        b = min(len(texts[i]), end - rs)
        t = texts[i]
        prefix, mid, suffix = t[:a], t[a:b], t[b:]
        if mid == "":
            continue
        parent = r.getparent()
        if prefix == "" and suffix == "":
            # 整个 run 转为删除
            d = make_del_from_run(r, rev)
            r.addprevious(d)
            parent.remove(r)
            last_del = d
            last_anchor = d
        else:
            if prefix == "":
                # run 开头被删：del 只含被删区间，剩余部分 clone 到后面
                d = make_del_element(mid, r, rev)
                r.addprevious(d)
                parent.remove(r)
                last_del = d
                if suffix != "":
                    sr = clone_run_with_text(r, suffix)
                    d.addnext(sr)
                    last_anchor = sr
                else:
                    last_anchor = d
            else:
                # 保留前缀，拆出 del 与后缀
                set_run_text(r, prefix)
                d = make_del_element(mid, r, rev)
                r.addnext(d)
                last_del = d
                if suffix != "":
                    sr = clone_run_with_text(r, suffix)
                    d.addnext(sr)
                    last_anchor = sr
                else:
                    last_anchor = d
    return (last_del is not None, last_del, last_anchor)


def find_run_at(p, offset: int):
    """
    返回段落 p 中覆盖偏移 offset 的 run（删除操作后的当前状态）。
    offset 在段末时返回 None。
    """
    runs = list(p.r_lst)
    if not runs:
        return None
    cur = 0
    for r in runs:
        ln = len(run_text(r))
        if offset < cur + ln:
            return r
        cur += ln
    return None


def insert_inline(p, start: int, text: str, rev: Revision) -> bool:
    """在段落 p 的偏移 start 处插入修订文本（inline）。start 指向锚点文本的结束位置。"""
    runs = list(p.r_lst)
    if not runs:
        # 空段落：直接追加
        r = p.makeelement(qn("w:r"), {})
        t = p.makeelement(qn("w:t"), {qn("xml:space"): "preserve"})
        t.text = text
        r.append(t)
        ins = p.makeelement(qn("w:ins"), _attr_map(rev, "ins"))
        ins.append(r)
        p.append(ins)
        return True

    # 找到 start 所在的 run（start == 某 run 起点时归入该 run）
    offsets = []
    cur = 0
    for r in runs:
        offsets.append(cur)
        cur += len(run_text(r))
    target_idx = None
    for i, r in enumerate(runs):
        if offsets[i] <= start <= offsets[i] + len(run_text(r)):
            target_idx = i
            break
    if target_idx is None:
        target_idx = len(runs) - 1

    target = runs[target_idx]
    tlen = len(run_text(target))
    rel = start - offsets[target_idx]

    if rel < tlen:
        # start 落在 run 内部：拆分，ins 插中间
        head = run_text(target)[:rel]
        tail = run_text(target)[rel:]
        set_run_text(target, head)
        ins = make_ins_run(text, target, rev)
        target.addnext(ins)
        if tail:
            sr = clone_run_with_text(target, tail)
            ins.addnext(sr)
    else:
        # start 恰在 run 末尾：直接插到 run 后
        ins = make_ins_run(text, target, rev)
        target.addnext(ins)
    return True


def insert_paragraph_after(p, text: str, rev: Revision):
    """在段落 p 后插入新段落（复制 p 的 pPr），内容为修订插入。"""
    new_p = copy.deepcopy(p)
    # 只保留 pPr，清除其它内容
    for child in list(new_p):
        if child.tag != qn("w:pPr"):
            new_p.remove(child)
    ref_r = None
    for r in p.r_lst:
        ref_r = r
        break
    if ref_r is None:
        # 原段 run 可能已全部转为 w:del，从 del 内取 rPr 作格式参考
        for node in p.iter(qn("w:r")):
            ref_r = node
            break
    if ref_r is None:
        ref_r = p  # 兜底
    ins = make_ins_run(text, ref_r, rev)
    new_p.append(ins)
    p.addnext(new_p)


def insert_table_row_after(anchor_p, cells_text, rev: Revision) -> bool:
    """
    表格行插入：在 anchor_p 所在表格行之后插入新行。
    cells_text 用 '|' 分隔各单元格文本。
    复制锚点行的格式（单元格数、宽度、字体样式）。
    anchor_p 是 w:p，向上找 w:tc -> w:tr。
    """
    tc = anchor_p.getparent()
    if tc is None or tc.tag != qn("w:tc"):
        return False
    tr = tc.getparent()
    if tr is None or tr.tag != qn("w:tr"):
        return False
    tbl = tr.getparent()
    if tbl is None or tbl.tag != qn("w:tbl"):
        return False

    new_tr = copy.deepcopy(tr)
    # 给新行加表格行插入修订标记（w:trPr/w:ins），使 rejected 视图不显示该行
    tr_pr = new_tr.find(qn("w:trPr"))
    if tr_pr is None:
        tr_pr = new_tr.makeelement(qn("w:trPr"), {})
        new_tr.insert(0, tr_pr)
    ins_mark = new_tr.makeelement(qn("w:ins"), _attr_map(rev, "ins"))
    tr_pr.append(ins_mark)
    # 清空新行所有单元格文本（保留结构/格式），依次写入 cells_text
    cells = new_tr.findall(qn("w:tc"))
    values = [v.strip() for v in cells_text.split("|")]
    for idx, tc_el in enumerate(cells):
        # 清除所有 run 文本（保留 tcPr / pPr 格式）
        for r in tc_el.iter(qn("w:r")):
            r.getparent().remove(r)
        # 取第一个段落写入新文本
        ps = tc_el.findall(qn("w:p"))
        if not ps:
            continue
        p0 = ps[0]
        r = p0.makeelement(qn("w:r"), {})
        t = p0.makeelement(qn("w:t"), {qn("xml:space"): "preserve"})
        t.text = values[idx] if idx < len(values) else ""
        r.append(t)
        p0.append(r)
    tr.addnext(new_tr)
    return True


def in_table(p) -> bool:
    """段落是否位于表格内（用于 INSERT 路由：表格行插入 vs 普通段落插入）。"""
    parent = p.getparent()
    return parent is not None and parent.tag == qn("w:tc")


# --------------------------------------------------- 精细替换（token 级 diff） ---

# 数值型 token：含千分位逗号、小数点、负号、百分号（半角/全角），作为整体参与 diff。
# 目的：保证 "1,742.24亿元"->"1,822.56亿元"、"1000"->"1100" 等数据「整体修订」
#       （整段 w:del + 整段 w:ins），而非只标变化尾数（如只标 742.24->822.56），便于核查。
_TOK_RE = re.compile(r'[一-鿿]|[-+]?[\d,]*\.?\d+[%％]?|[0-9A-Za-z.]+|[^一-鿿0-9A-Za-z.]')


def _tokenize(s: str):
    """按 token 切分：CJK 单字 / 数值整体（含千分位、小数点、负号、百分号）/ 连续字母数字点 / 其它单字符。"""
    return _TOK_RE.findall(s)


def _is_numeric_token(tok: str) -> bool:
    """判断 token 是否为数值型（用于诊断/可读性，diff 已靠正则切分保证整体）。"""
    return bool(re.match(r'^[-+]?[\d,]*\.?\d+[%％]?$', tok))


def _split_at(p, offset: int):
    """在段落 p 的偏移 offset 处拆分 run，使其成为 run 边界（就地修改）。"""
    cur = 0
    for r in list(p.r_lst):
        ln = len(run_text(r))
        if cur < offset < cur + ln:
            rel = offset - cur
            txt = run_text(r)
            set_run_text(r, txt[:rel])
            if txt[rel:]:
                sr = clone_run_with_text(r, txt[rel:])
                r.addnext(sr)
            return
        cur += ln


def _normal_run(p, rpr, text: str):
    r = p.makeelement(qn("w:r"), {})
    if rpr is not None:
        r.append(copy.deepcopy(rpr))
    t = p.makeelement(qn("w:t"), {qn("xml:space"): "preserve"})
    t.text = text
    r.append(t)
    return r


def _del_run(p, rpr, text: str, rev: Revision):
    del_el = p.makeelement(qn("w:del"), _attr_map(rev, "del"))
    r = p.makeelement(qn("w:r"), {})
    if rpr is not None:
        r.append(copy.deepcopy(rpr))
    dt = p.makeelement(qn("w:delText"), {})
    dt.text = text
    r.append(dt)
    del_el.append(r)
    return del_el


def _ins_run(p, rpr, text: str, rev: Revision):
    ins_el = p.makeelement(qn("w:ins"), _attr_map(rev, "ins"))
    r = p.makeelement(qn("w:r"), {})
    if rpr is not None:
        r.append(copy.deepcopy(rpr))
    t = p.makeelement(qn("w:t"), {qn("xml:space"): "preserve"})
    t.text = text
    r.append(t)
    ins_el.append(r)
    return ins_el


def _bold_prefix_split(p):
    """
    识别「段首加粗」模式：返回 (bold_len, bold_rpr, plain_rpr)。
    - 旧报告常有 "信托业务：…" 前缀加粗，或整段加粗（总起句段）。
    - 若段落首部存在连续加粗 run 且其后存在非加粗 run，则 bold_len = 首部加粗文本长度；
      若整段加粗，bold_len = 全段长度（即插入文本亦应全段加粗，分拆无意义）。
    无加粗则不返回 (0, None, None)。
    """
    runs = [r for r in p.r_lst if run_text(r)]
    if not runs:
        return (0, None, None)
    flags = []
    for r in runs:
        rpr = r.find(qn("w:rPr"))
        b = rpr.find(qn("w:b")) if rpr is not None else None
        is_bold = b is not None and (b.get(qn("w:val")) not in ("0", "false"))
        flags.append(is_bold)
    if not any(flags):
        return (0, None, None)
    first_plain = next((i for i, f in enumerate(flags) if not f), None)
    if first_plain is None:
        # 整段加粗：保持全段加粗，无需分拆
        return (0, None, None)
    if not all(flags[:first_plain]):
        return (0, None, None)
    bold_len = sum(len(run_text(r)) for r in runs[:first_plain])
    if bold_len == 0:
        return (0, None, None)
    return (bold_len, _copy_rpr(runs[0]), _copy_rpr(runs[first_plain]))


def replace_span_whole(p, start: int, end: int, new_text: str, rev: Revision,
                       segments=None) -> bool:
    """
    整体替换：将 [start,end) 文本整段标 w:del，其后整段插 w:ins(new_text)。
    用于新旧文本结构差异大、token 级 diff 会产生乱码（如定性整句改写）的场景。

    格式处理（三级）：
    1) 若 action 显式给出 segments=[{"text":..,"bold":bool}, ...]，按段逐段构造 run
       （bold=True 用段落加粗前缀的 rPr，bold=False 用非加粗正文的 rPr），
       用于「段首总起句本身也被改写」的场景（此时无法靠前缀逐字相同来判定分拆）。
    2) 否则若原段落为「段首加粗」样式且插入文本的加粗前缀与原文逐字相同，
       按原加粗前缀长度拆为「加粗前缀 + 非加粗正文」两 run。
    3) 否则用「span 起始位置所在 run」的 rPr（不再误用段落首个 run 的 rPr，
       避免局部替换的插入文本错误继承加粗）。
    """
    ref = find_run_at(p, start)
    rpr = _copy_rpr(ref) if ref is not None else None
    if rpr is None:
        for r in list(p.r_lst):
            if run_text(r):
                rpr = _copy_rpr(r)
                break
    # 若 span 覆盖整段，检测段首加粗，准备分拆格式（须在删除前取原文）
    bold_len, bold_rpr, plain_rpr = (0, None, None)
    orig_span_text = None
    if start == 0 and end >= len(para_plain_text(p)):
        bold_len, bold_rpr, plain_rpr = _bold_prefix_split(p)
        orig_span_text = para_plain_text(p)[start:end]

    ok, last_del, _ = mark_delete_range(p, start, end, rev)
    if not ok or last_del is None:
        return False

    ins_el = p.makeelement(qn("w:ins"), _attr_map(rev, "ins"))

    def _run(rpr_ref, text):
        r = p.makeelement(qn("w:r"), {})
        if rpr_ref is not None:
            r.append(copy.deepcopy(rpr_ref))
        t = p.makeelement(qn("w:t"), {qn("xml:space"): "preserve"})
        t.text = text
        r.append(t)
        return r

    if segments:
        for seg in segments:
            txt = seg.get("text", "")
            if txt == "":
                continue
            if seg.get("bold"):
                seg_ref = bold_rpr if bold_rpr is not None else rpr
            else:
                # 明确「不加粗」：plain_rpr 为 None（原正文 run 无 rPr）时不得回落到
                # 加粗 rpr，否则整段会继承段首汇总句的粗体
                seg_ref = plain_rpr
                if seg_ref is not None:
                    seg_ref = copy.deepcopy(seg_ref)
                    b = seg_ref.find(qn("w:b"))
                    if b is None:
                        b = seg_ref.makeelement(qn("w:b"), {})
                        seg_ref.insert(0, b)
                    b.set(qn("w:val"), "0")
            ins_el.append(_run(seg_ref, txt))
    elif (bold_len and 0 < bold_len < len(new_text) and orig_span_text
            and new_text[:bold_len] == orig_span_text[:bold_len]):
        # 段首加粗：前缀保持粗体，正文用非加粗 run
        ins_el.append(_run(bold_rpr, new_text[:bold_len]))
        ins_el.append(_run(plain_rpr, new_text[bold_len:]))
    else:
        ins_el.append(_run(rpr, new_text))

    last_del.addnext(ins_el)
    return True


def replace_span_with_diff(p, start: int, end: int, new_text: str, rev: Revision) -> bool:
    """
    将段落 p 的 [start,end) 文本（当前实际文本）按 token 级 diff 替换为 new_text：
    - 未变化的 token 保持为普通文本（不带修订标记）
    - 变化的 token 才标 w:del / w:ins
    返回是否成功。失败时回退为整段删除+插入（保证不丢更新）。
    """
    if start >= end:
        return False
    # 1. 将 start/end 对齐到 run 边界
    _split_at(p, start)
    _split_at(p, end)
    # 2. 收集 span runs，构建逐字符 rPr 映射
    cur = 0
    span_runs = []
    rpr_per_char = []
    for r in list(p.r_lst):
        ln = len(run_text(r))
        rs, re_ = cur, cur + ln
        if rs >= start and re_ <= end:
            span_runs.append(r)
            rpr = _copy_rpr(r)
            for _ in range(ln):
                rpr_per_char.append(rpr)
        cur += ln
    if not span_runs:
        return False
    old_text = "".join(run_text(r) for r in span_runs)
    if old_text == "":
        return False
    try:
        # 3. token 级 diff
        otoks = _tokenize(old_text)
        ntoks = _tokenize(new_text)
        sm = difflib.SequenceMatcher(None, otoks, ntoks, autojunk=False)
        ostart = []
        acc = 0
        for t in otoks:
            ostart.append(acc)
            acc += len(t)
        # 4. 构造新元素序列
        def _rpr_at(idx: int):
            """取「改动位置原字符」的 rPr 作为插入文本格式参考。

            不能用前一个字符（ins_pos-1）：当改动点恰在加粗↔非加粗边界时
            （如加粗总起句之后紧接的正文首字被替换），会错误继承相邻 run 的粗体。
            """
            if not rpr_per_char:
                return None
            if idx < len(otoks):
                return rpr_per_char[ostart[idx]]
            return rpr_per_char[-1]

        new_els = []
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                # 连续 equal token 按源 run rPr 分组为普通 run
                cur_rpr = None
                cur_txt = ""
                for k in range(i1, i2):
                    rpr = rpr_per_char[ostart[k]]
                    tok = otoks[k]
                    if rpr is cur_rpr:
                        cur_txt += tok
                    else:
                        if cur_txt:
                            new_els.append(_normal_run(p, cur_rpr, cur_txt))
                        cur_txt = tok
                        cur_rpr = rpr
                if cur_txt:
                    new_els.append(_normal_run(p, cur_rpr, cur_txt))
            elif tag == "delete":
                txt = "".join(otoks[i1:i2])
                rpr = rpr_per_char[ostart[i1]] if i1 < len(otoks) else None
                new_els.append(_del_run(p, rpr, txt, rev))
            elif tag == "insert":
                txt = "".join(ntoks[j1:j2])
                new_els.append(_ins_run(p, _rpr_at(i1), txt, rev))
            elif tag == "replace":
                dtxt = "".join(otoks[i1:i2])
                itxt = "".join(ntoks[j1:j2])
                drpr = rpr_per_char[ostart[i1]] if i1 < len(otoks) else None
                new_els.append(_del_run(p, drpr, dtxt, rev))
                new_els.append(_ins_run(p, _rpr_at(i1), itxt, rev))
        # 5. 替换 span runs
        anchor = span_runs[0].getprevious()
        for r in span_runs:
            p.remove(r)
        prev = anchor
        for el in new_els:
            if prev is None:
                p.insert(0, el)
            else:
                prev.addnext(el)
            prev = el
        return True
    except Exception:
        # 兜底：整段删除 + 段后插入，保证更新不丢
        ok, _, _ = mark_delete_range(p, start, end, rev)
        if ok and new_text:
            ref = p.r_lst[-1] if p.r_lst else p
            ins = make_ins_run(new_text, ref, rev)
            p.append(ins)
        return ok


# ---------------------------------------------------------------- 主流程 ---

def apply_actions(doc, actions, rev: Revision):
    applied, failed, reviewed = [], [], []
    for act in actions:
        aid = act.get("action_id") or act.get("id") or "?"
        atype = act.get("action", "").upper()
        orig = (act.get("original_text") or "").strip()
        new_text = act.get("new_text") or ""
        # new_text 为空时回落到 new_segments 拼接文本（供 diff 模式使用；
        # whole 模式仍优先按 segments 分段控制加粗，不受影响）
        if not new_text and act.get("new_segments"):
            new_text = "".join(s.get("text", "") for s in act["new_segments"])

        if atype == "REVIEW":
            reviewed.append(aid)
            continue

        if atype not in ("REPLACE", "INSERT", "DELETE"):
            failed.append(aid)
            continue

        loc = locate_paragraph(doc, orig)
        if loc is None:
            failed.append(aid)
            continue
        p, start, end, mode = loc

        if atype == "DELETE":
            ok, _, _ = mark_delete_range(p, start, end, rev)
            if ok:
                applied.append(aid)
            else:
                failed.append(aid)

        elif atype == "REPLACE":
            # 精细替换：仅真正变化的 token 标 w:del/w:ins，未变化部分保持普通文本
            if act.get("mode") == "whole" or act.get("whole"):
                ok = replace_span_whole(p, start, end, new_text, rev,
                                        segments=act.get("new_segments"))
            else:
                ok = replace_span_with_diff(p, start, end, new_text, rev)
            if ok:
                applied.append(aid)
            else:
                failed.append(aid)

        elif atype == "INSERT":
            if in_table(p):
                # 锚点在表格内：只允许表格行插入（new_text 含 |），否则记失败
                if "|" in new_text:
                    ok_row = insert_table_row_after(p, new_text, rev)
                    if ok_row:
                        applied.append(aid)
                        continue
                    failed.append(aid)
                    continue
                failed.append(aid)
                continue
            if mode == "full":
                # 锚点=整段 → 段落后插入新段
                insert_paragraph_after(p, new_text, rev)
            else:
                insert_inline(p, end, new_text, rev)
            applied.append(aid)

    return applied, failed, reviewed


def main() -> None:
    parser = argparse.ArgumentParser(description="应用 Action List 生成修订模式 docx")
    parser.add_argument("--original", required=True, help="旧报告 docx 路径")
    parser.add_argument("--actions", required=True, help="Action List JSON 路径")
    parser.add_argument("--output", default=None, help="修订版 docx 输出路径")
    parser.add_argument("--log", default=None, help="execution_log JSON 输出路径")
    parser.add_argument("--author", default="WorkBuddy", help="修订作者名（默认 WorkBuddy）")
    args = parser.parse_args()

    original_path = Path(args.original)
    actions_path = Path(args.actions)
    if not original_path.exists():
        sys.exit(f"旧报告不存在: {original_path}")
    if not actions_path.exists():
        sys.exit(f"Action List 不存在: {actions_path}")

    with actions_path.open(encoding="utf-8") as f:
        payload = json.load(f)
    actions = payload.get("actions", payload) if isinstance(payload, dict) else payload
    if not isinstance(actions, list):
        sys.exit("Action List 格式错误：应为 actions 数组")

    doc = Document(str(original_path))
    rev = Revision(args.author)
    applied, failed, reviewed = apply_actions(doc, actions, rev)

    output_path = Path(args.output) if args.output else (
        original_path.with_name(f"{original_path.stem}_修订版.docx"))
    doc.save(str(output_path))

    executable = [a for a in actions if a.get("action", "").upper() in ("REPLACE", "INSERT", "DELETE")]
    log = {
        "total_executable_actions": len(executable),
        "applied_actions": applied,
        "failed_actions": failed,
        "review_actions_untouched": reviewed,
        "output_file": str(output_path),
        "author": args.author,
    }
    if args.log:
        Path(args.log).write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(log, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
