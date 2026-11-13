"""内评简评报告 docx 生成器（与模板 v1.1 一一对应）。

只依赖 python-docx。输入一个符合 01_模板/内评简评报告通用模板_v1.1.json 的
数据结构，输出 .docx，版式按参考件（人工撰写的同类报告）对齐：

- 中文楷体_GB2312、五号(10.5pt)、行距1.15、首行缩进2字符、段前段后0
- 标题行 / 正面： / 关注： / 1、基本情况 / 2、点评 / 汇总句 / 表题 加粗
- 段落之间插入一个空段作为分块间隔（参考件即如此，否则整页挤成一块）
- 表格：外框上下 1.5pt、内部横竖线 0.5pt、左右不封口；表头加粗居中，
  数值列右对齐、首列左对齐；行高 0.4cm 起、单元格垂直居中、禁止跨页断行
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.table import WD_ROW_HEIGHT_RULE, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt

FONT_CN = "楷体_GB2312"
FONT_SIZE_PT = 10.5
TEXT_WIDTH_CM = 14.66          # A4 21cm - 左右各 3.17cm
ROW_HEIGHT_TWIPS = 227         # 参考件行高 0.4cm（atLeast）

_TBLPR_TAIL = ("w:shd", "w:tblLayout", "w:tblCellMar", "w:tblLook",
               "w:tblCaption", "w:tblDescription", "w:tblPrChange")


def _end_period(text: str) -> str:
    """摘要条目统一以句号收尾，避免生成稿出现无标点短句。"""
    t = (text or "").strip()
    return t if not t or t[-1] in "。！？；" else t + "。"


def _set_run_font(run, bold: bool = False, size_pt: float = FONT_SIZE_PT):
    run.font.name = FONT_CN
    run.font.size = Pt(size_pt)
    run.bold = bold
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    rfonts.set(qn("w:eastAsia"), FONT_CN)
    rfonts.set(qn("w:ascii"), FONT_CN)
    rfonts.set(qn("w:hAnsi"), FONT_CN)


# w:pPr 的子元素必须按这个顺序出现，OOXML schema 是严格顺序的。
# 顺序错了 Word 不报错，只是**默默忽略**排在错误位置的那些属性——
# 缩进、行距、对齐全按默认值走，看起来就是「版式没生效」。
_PPR_ORDER = (
    "w:pStyle", "w:keepNext", "w:keepLines", "w:pageBreakBefore", "w:framePr",
    "w:widowControl", "w:numPr", "w:suppressLineNumbers", "w:pBdr", "w:shd",
    "w:tabs", "w:suppressAutoHyphens", "w:kinsoku", "w:wordWrap",
    "w:overflowPunct", "w:topLinePunct", "w:autoSpaceDE", "w:autoSpaceDN",
    "w:bidi", "w:adjustRightInd", "w:snapToGrid", "w:spacing", "w:ind",
    "w:contextualSpacing", "w:mirrorIndents", "w:suppressOverlap", "w:jc",
    "w:textDirection", "w:textAlignment", "w:textboxTightWrap", "w:outlineLvl",
    "w:divId", "w:cnfStyle", "w:rPr", "w:sectPr", "w:pPrChange",
)


def _ppr_put(ppr, el, tag: str):
    """按 schema 顺序把 el 放进 pPr，已有的同标签先删掉。"""
    for old in ppr.findall(qn(tag)):
        ppr.remove(old)
    try:
        succ = _PPR_ORDER[_PPR_ORDER.index(tag) + 1:]
    except ValueError:
        ppr.append(el)
        return el
    ppr.insert_element_before(el, *succ)
    return el


def _indent_two_chars(paragraph):
    """真正的『首行缩进 2 字符』（firstLineChars=200），与 Word 行为一致。"""
    ppr = paragraph._p.get_or_add_pPr()
    ind = OxmlElement("w:ind")
    ind.set(qn("w:firstLineChars"), "200")
    ind.set(qn("w:firstLine"), str(int(FONT_SIZE_PT * 2 * 20)))
    _ppr_put(ppr, ind, "w:ind")


def _fmt_paragraph(paragraph, align=None):
    pf = paragraph.paragraph_format
    pf.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
    pf.line_spacing = 1.15
    pf.space_before = Pt(0)
    pf.space_after = Pt(0)
    # 正文默认两端对齐：中文段落右边缘必须齐，否则满行参差。
    # 标题、表格数字等由调用方显式传 align 覆盖，不受影响。
    paragraph.alignment = align if align is not None else WD_ALIGN_PARAGRAPH.JUSTIFY
    _indent_two_chars(paragraph)
    _no_snap(paragraph)


def _no_snap(paragraph):
    """关掉「与网格对齐」。

    文档里带行网格（docGrid linePitch）时，段落的行高会被向上取整到网格的
    整数倍：10.5pt 字、1.15 倍行距本是 12.1pt，落在 18pt 网格上就变成 18pt，
    读者看到的是「行距忽大忽小、比设定松很多」。正文一律不吸附网格。
    """
    ppr = paragraph._p.get_or_add_pPr()
    snap = OxmlElement("w:snapToGrid")
    snap.set(qn("w:val"), "0")
    _ppr_put(ppr, snap, "w:snapToGrid")


def _strip_indent(paragraph):
    """把缩进显式清零。标题、单位行、空段、表格单元格都不该缩进。

    **必须是「设成 0」而不是「删掉 w:ind」**。Normal 样式本身带
    firstLineChars=200，段落没有自己的 w:ind 时就从样式继承——删掉等于
    什么都没做。删的那一版让表格每格都缩进 2 字符，36pt 的列只剩 12pt 可用，
    「75.77%」这种六位数字全被折成两行。
    """
    ppr = paragraph._p.get_or_add_pPr()
    ind = OxmlElement("w:ind")
    for attr in ("w:firstLine", "w:firstLineChars", "w:left", "w:leftChars",
                 "w:right", "w:rightChars", "w:hanging", "w:hangingChars"):
        ind.set(qn(attr), "0")
    _ppr_put(ppr, ind, "w:ind")


def _setup_styles(doc):
    """把版式写进 Normal 样式，而不只是逐个段落设直接格式。

    这一步是「导出后自己编辑才变样」的根治办法。以前只在每个段落上设
    spacing/ind/jc（直接格式），Normal 样式里只有字体与字号，于是：
    用户在 Word 里回车新建一段、或把别处的文字粘进来，新段落继承的是
    **Normal + docDefaults**——段后 10pt（docDefaults 的 w:spacing after=200）、
    无首行缩进、无两端对齐。屏幕上原本整齐的报告，一编辑就散。
    样式设好之后，新建段落与「只保留文本」粘贴都自动落到同一套版式上。
    """
    # ① docDefaults：兜底的那一层。python-docx 默认模板给了段后 200 twips(10pt)，
    #    正是「粘贴进来段前段后有奇怪间距」的来源。
    styles_el = doc.styles.element
    dd = styles_el.find(qn("w:docDefaults"))
    if dd is not None:
        ppr_default = dd.find(qn("w:pPrDefault"))
        if ppr_default is not None:
            ppr = ppr_default.find(qn("w:pPr"))
            if ppr is None:
                ppr = OxmlElement("w:pPr")
                ppr_default.append(ppr)
            sp = OxmlElement("w:spacing")
            sp.set(qn("w:before"), "0")
            sp.set(qn("w:after"), "0")
            sp.set(qn("w:line"), "276")          # 1.15 × 240
            sp.set(qn("w:lineRule"), "auto")
            _ppr_put(ppr, sp, "w:spacing")

    # ② Normal 样式：Word 里回车得到的段落就是它
    normal = doc.styles["Normal"]
    normal.font.name = FONT_CN
    normal.font.size = Pt(FONT_SIZE_PT)
    npr = normal.element.get_or_add_rPr()
    rfonts = npr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        npr.insert(0, rfonts)
    for attr in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
        rfonts.set(qn(attr), FONT_CN)
    # 去掉主题字体引用：留着 asciiTheme 会覆盖上面的 rFonts
    for attr in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
        if rfonts.get(qn(attr)) is not None:
            del rfonts.attrib[qn(attr)]
    szcs = npr.find(qn("w:szCs"))
    if szcs is None:
        szcs = OxmlElement("w:szCs")
        npr.append(szcs)
    szcs.set(qn("w:val"), str(int(FONT_SIZE_PT * 2)))

    # 样式里的 pPr 同样受 schema 顺序约束，一律走 _ppr_put
    ppr = normal.element.get_or_add_pPr()
    snap = OxmlElement("w:snapToGrid")
    snap.set(qn("w:val"), "0")
    _ppr_put(ppr, snap, "w:snapToGrid")
    spacing = OxmlElement("w:spacing")
    spacing.set(qn("w:before"), "0")
    spacing.set(qn("w:after"), "0")
    spacing.set(qn("w:line"), "276")
    spacing.set(qn("w:lineRule"), "auto")
    _ppr_put(ppr, spacing, "w:spacing")
    ind = OxmlElement("w:ind")
    ind.set(qn("w:firstLineChars"), "200")
    ind.set(qn("w:firstLine"), str(int(FONT_SIZE_PT * 2 * 20)))
    _ppr_put(ppr, ind, "w:ind")
    jc = OxmlElement("w:jc")
    jc.set(qn("w:val"), "both")
    _ppr_put(ppr, jc, "w:jc")

    # ③ 页面：去掉行网格。中文排版里「文档网格」会把行距按网格取整，
    #    与 1.15 倍行距打架；设定行距要生效，就得让它退出这条规则。
    for sec in doc.sections:
        sectPr = sec._sectPr
        for grid in sectPr.findall(qn("w:docGrid")):
            grid.set(qn("w:type"), "none")
            if grid.get(qn("w:linePitch")) is None:
                grid.set(qn("w:linePitch"), "312")
            for attr in ("w:charSpace",):
                if grid.get(qn(attr)) is not None:
                    del grid.attrib[qn(attr)]


def _add_para(doc, text: str, bold: bool = False, align=None, indent: bool = True):
    p = doc.add_paragraph()
    _fmt_paragraph(p, align)
    if not indent:
        _strip_indent(p)
    r = p.add_run(text)
    _set_run_font(r, bold=bold)
    return p


def _add_blank(doc):
    """块之间的空行。参考件用空段落分隔，不靠段前段后间距。

    空段落要去掉首行缩进：缩进对空段本身看不出来，但用户在里面打一个字，
    那一行就会莫名缩进两格——看起来像版式坏了。
    """
    p = doc.add_paragraph()
    _fmt_paragraph(p)
    _strip_indent(p)
    return p


def _add_lead_para(doc, lead: str, body: str, bold_lead: bool = True, align=None):
    """段首加粗汇总句 + 常规正文。"""
    p = doc.add_paragraph()
    _fmt_paragraph(p, align)
    if lead:
        r1 = p.add_run(lead)
        _set_run_font(r1, bold=bold_lead)
    if body:
        r2 = p.add_run(body)
        _set_run_font(r2, bold=False)
    return p


def _cell_text(cell, text: str, bold=False, align=WD_ALIGN_PARAGRAPH.RIGHT,
               size_pt: float = FONT_SIZE_PT):
    cell.text = ""
    p = cell.paragraphs[0]
    pf = p.paragraph_format
    pf.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
    pf.line_spacing = 1.15
    pf.space_before = Pt(0)
    pf.space_after = Pt(0)
    p.alignment = align
    # 单元格段落会继承 Normal 的首行缩进 2 字符——不清掉的话表里每个格子
    # 都从第二格位置起排，看着像缩进错乱。网格同理。
    _strip_indent(p)
    _no_snap(p)
    r = p.add_run(str(text))
    _set_run_font(r, bold=bold, size_pt=size_pt)
    # 单元格垂直居中，与参考件一致
    tcPr = cell._tc.get_or_add_tcPr()
    valign = tcPr.find(qn("w:vAlign"))
    if valign is None:
        valign = OxmlElement("w:vAlign")
        tcPr.append(valign)
    valign.set(qn("w:val"), "center")


def _tbl_borders(tblPr):
    """外框上下 1.5pt、左右不画；内部横竖线 0.5pt。与参考件一致。"""
    for old in tblPr.findall(qn("w:tblBorders")):
        tblPr.remove(old)
    borders = OxmlElement("w:tblBorders")
    for edge, val, sz in (("top", "single", 12), ("left", "none", 0),
                          ("bottom", "single", 12), ("right", "none", 0),
                          ("insideH", "single", 4), ("insideV", "single", 4)):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:val"), val)
        el.set(qn("w:sz"), str(sz))
        el.set(qn("w:space"), "0")
        el.set(qn("w:color"), "000000")
        borders.append(el)
    tblPr.insert_element_before(borders, *_TBLPR_TAIL)


def _tbl_cell_margins(tblPr, dxa: int = 108):
    for old in tblPr.findall(qn("w:tblCellMar")):
        tblPr.remove(old)
    mar = OxmlElement("w:tblCellMar")
    for side, w in (("top", 0), ("left", dxa), ("bottom", 0), ("right", dxa)):
        el = OxmlElement(f"w:{side}")
        el.set(qn("w:w"), str(w))
        el.set(qn("w:type"), "dxa")
        mar.append(el)
    tblPr.insert_element_before(mar, "w:tblLook", "w:tblCaption",
                                "w:tblDescription", "w:tblPrChange")


def _tbl_width(tblPr, cm: float):
    el = tblPr.find(qn("w:tblW"))
    if el is None:
        el = OxmlElement("w:tblW")
        tblPr.insert_element_before(el, "w:jc", "w:tblCellSpacing", "w:tblInd",
                                    *_TBLPR_TAIL)
    el.set(qn("w:type"), "dxa")
    el.set(qn("w:w"), str(int(round(cm * 567))))


def _row_props(row, header: bool = False):
    trPr = row._tr.get_or_add_trPr()
    if trPr.find(qn("w:cantSplit")) is None:
        trPr.append(OxmlElement("w:cantSplit"))
    if header and trPr.find(qn("w:tblHeader")) is None:
        trPr.append(OxmlElement("w:tblHeader"))
    row.height = Pt(ROW_HEIGHT_TWIPS / 20.0)
    row.height_rule = WD_ROW_HEIGHT_RULE.AT_LEAST


def _add_table(doc, header, rows, unit_line=None, col_widths_cm=None, bold_rows=(),
               groups=None, extra_cols=None, first_col=None):
    """表头支持两种形状：单行 header；或 groups（期次分组）+ sub_cols 的两行表头。

    两行表头在 Word 里要真合并单元格（gridSpan / vMerge），不能只把字排两行——
    后者在读者眼里是「第一行空了半截」，看不出哪几列属于哪一期。
    """
    if unit_line:
        up = _add_para(doc, unit_line, align=WD_ALIGN_PARAGRAPH.RIGHT, indent=False)
        up.paragraph_format.keep_with_next = True  # 单位行不单独留在页尾
    two_row = bool(groups and header)
    ncols = ((1 + sum(len(g.get("cols") or []) for g in groups) + len(extra_cols or []))
             if two_row else (len(header) if header else max(len(r) for r in rows)))

    # ── 先定字号与列宽，再建表填内容 ──
    # 顺序不能反：cell 里的字号一旦写死在 run 上，事后再改就得遍历所有 run。
    # 而字号又必须由「这些内容放不放得下」决定，所以决策只能前置。
    cell_pt = _table_font(ncols)
    cell_dxa = _table_margin(ncols)
    widths = list(col_widths_cm or [])
    if len(widths) != ncols:
        hdr_texts = None
        if two_row:
            hdr_texts = [first_col or "业务板块"]
            for g in groups:
                hdr_texts.extend(g.get("cols") or [])
            hdr_texts.extend(extra_cols or [])
        widths, auto_pt = _auto_col_widths(
            ncols, first_col or (header[0] if header else ""),
            rows, extra_cols or [], hdr_texts, bold_rows)
        if auto_pt < cell_pt:                     # 装不下才降字号
            cell_pt = auto_pt
    total = sum(widths)
    if total > TEXT_WIDTH_CM:                     # 按版心等比压缩，避免超出页边距
        widths = [round(w * TEXT_WIDTH_CM / total, 2) for w in widths]

    table = doc.add_table(rows=0, cols=ncols)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER

    if two_row:
        r1 = table.add_row().cells
        r2 = table.add_row().cells
        c0 = r1[0].merge(r2[0])
        _cell_text(c0, first_col or "业务板块", bold=True, align=WD_ALIGN_PARAGRAPH.CENTER, size_pt=cell_pt)
        pos = 1
        for g in groups:
            cols = g.get("cols") or []
            span = max(1, len(cols))
            cell = r1[pos] if span == 1 else r1[pos].merge(r1[pos + span - 1])
            _cell_text(cell, g.get("label") or "", bold=True, align=WD_ALIGN_PARAGRAPH.CENTER, size_pt=cell_pt)
            for j, cname in enumerate(cols):
                _cell_text(r2[pos + j], cname, bold=False, align=WD_ALIGN_PARAGRAPH.CENTER, size_pt=cell_pt)
            pos += span
        for k, cname in enumerate(extra_cols or []):
            cell = r1[pos + k].merge(r2[pos + k])
            _cell_text(cell, cname, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER, size_pt=cell_pt)
    elif header:
        cells = table.add_row().cells
        for i, h in enumerate(header):
            _cell_text(cells[i], h, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER, size_pt=cell_pt)

    for idx, row in enumerate(rows):
        cells = table.add_row().cells
        for i in range(ncols):
            val = row[i] if i < len(row) else ""
            # 长文本（如项目名、板块名）左对齐，纯数值右对齐 → 数值列才能看齐
            is_num = bool(val) and all(ch.isdigit() or ch in ".,%-—()（） " for ch in str(val))
            align = WD_ALIGN_PARAGRAPH.RIGHT if (i > 0 and is_num) else WD_ALIGN_PARAGRAPH.LEFT
            _cell_text(cells[i], val, bold=(idx in bold_rows), align=align, size_pt=cell_pt)

    tblPr = table._tbl.tblPr
    _tbl_borders(tblPr)
    _tbl_cell_margins(tblPr, cell_dxa)
    _tbl_width(tblPr, sum(widths))
    table.autofit = False

    for i, w in enumerate(widths):
        table.columns[i].width = Cm(w)
    hdr_rows = 2 if two_row else (1 if header else 0)
    for idx, row in enumerate(table.rows):
        _row_props(row, header=(idx < hdr_rows))
        for i, w in enumerate(widths):
            if i < len(row.cells):
                row.cells[i].width = Cm(w)
    return table


def _char_w(ch: str) -> float:
    """一个字符大约占多少厘米（10.5pt 楷体）。中文按全角、西文数字按半角。"""
    return 0.372 if ord(ch) > 0x2E80 else 0.20


def _text_cm(text: str) -> float:
    return sum(_char_w(c) for c in str(text or ""))


def _table_font(ncols: int) -> float:
    """表格字号随列数降档。

    业务构成表四期就是 10 列，A4 纵向版心只有 14.66cm，每列 1.3cm 上下。
    主表用的 10.5pt 在那儿放不下「75.77%」这样的六位数字，会折成两行。
    表内字号降到 8.5pt 是行业惯例（评级报告的附表普遍 8—9pt）。
    """
    if ncols <= 6:
        return FONT_SIZE_PT
    if ncols <= 8:
        return 9.5
    return 8.5


def _table_margin(ncols: int) -> int:
    """单元格左右边距（twips）。列多时收到 1mm，把宽度让给内容。"""
    if ncols <= 6:
        return 108
    if ncols <= 8:
        return 72
    return 57


def _auto_col_widths(ncols: int, first_head: str, rows: list, extra_cols: list,
                     header_texts: list | None = None,
                     bold_rows: list | None = None) -> tuple[list[float], float]:
    """按每列的实际内容分配宽度，并把字号一并定下来（返回值只用宽度）。

    这里的关键是**先保证每列放得下自己的内容，再把富余分出去**。
    早先的写法是「平均分，超了就压中间列」，压到比内容还窄，数字于是折成
    两行——一张表里每个格子都折行，看着像排版坏了，其实只是宽度给少了。
    """
    body = [r for r in (rows or []) if r]
    cell_mar = _table_margin(ncols) / 567.0        # 左右边距之和，cm
    extra_n = len(extra_cols or [])
    mid_idx = list(range(1, ncols - extra_n))
    bold_set = set(int(x) for x in (bold_rows or []))
    # 「营业总收入」行按惯例加粗，无论调用方有没有传 bold_rows——宽度估算
    # 要与实际渲染一致，否则留的余量正好差这一行。
    for i, r in enumerate(body):
        if str(r[0] if r else "").strip() in ("营业总收入", "合计", "总计", "收入合计"):
            bold_set.add(i)

    def measure(pt: float):
        """按某个字号算各列需要多宽。"""
        scale = pt / FONT_SIZE_PT
        # 加粗行（营业总收入）的字形比常规粗一档，同一串数字占的宽度约多 6%。
        # 不留这一点余量，恰恰是「合计行」那几个数被挤成两行——普通行好好的，
        # 唯独最关键的一行散架，看着最刺眼。
        bold_extra = 1.06

        def w(text, bold=False) -> float:
            # 0.372cm/汉字 是按 10.5pt 估的，中文字距与「%」这类符号在部分字体
            # 里按全角排，实测总比估算宽一点点；留 8% 免得总是差最后一两个字。
            v = _text_cm(str(text or "")) * scale * (bold_extra if bold else 1.0) * 1.08
            return v + cell_mar

        # 首列：名字太长就折行，宽度封顶
        first = max([w(first_head or "业务板块", True)] +
                    [w(r[0], i in bold_set) for i, r in enumerate(body)] or [0])
        first_w = min(max(first, 2.2), 3.2 if ncols > 8 else 5.6)

        # 数值列统一取同一个宽度：长短不一的三列并排，表格右半截会显得参差。
        # 取所有数值列（含各自的表头）里最宽的那一个。
        per = 0.9
        for i in mid_idx:
            for ri, r in enumerate(body):
                if i < len(r):
                    per = max(per, w(r[i], ri in bold_set))
            if header_texts and i < len(header_texts):
                per = max(per, w(header_texts[i], True))
        mid = [per] * len(mid_idx)

        extra = [max(w(lb, True) + 0.22, 1.5) for lb in (extra_cols or [])]
        return first_w, mid, extra

    # 从大到小试字号，选第一个装得下的。
    # **选定的字号必须回传给排版**：早先这里按 8pt 算宽、排版却写 8.5pt，
    # 文字比预留的宽半档，占比列于是折成「75.77／%」这种两行。
    chosen = _table_font(ncols)
    first_w = mid = extra = None
    for pt in (FONT_SIZE_PT, 9.5, 9, 8.5, 8):
        if ncols > 6 and pt > _table_font(ncols):
            continue                              # 列多时不试大字号，省得白算
        f, m, e = measure(pt)
        if f + sum(m) + sum(e) <= TEXT_WIDTH_CM:
            first_w, mid, extra, chosen = f, m, e, pt
            break
    if first_w is None:                           # 全都装不下：取最小字号，按比例压
        first_w, mid, extra = measure(8)
        total = first_w + sum(mid) + sum(extra)
        k = TEXT_WIDTH_CM / total
        first_w, mid, extra = first_w * k, [x * k for x in mid], [x * k for x in extra]
        chosen = 8

    # 富余按各列需求的占比分给数值列（不碰首列与毛利率列，免得失衡）
    diff = TEXT_WIDTH_CM - (first_w + sum(mid) + sum(extra))
    if diff > 0.02 and mid:
        share = diff / len(mid)
        mid = [x + share for x in mid]

    cols = [round(first_w, 2)] + [round(x, 2) for x in mid] + [round(x, 2) for x in extra]
    left = TEXT_WIDTH_CM - sum(cols)
    if left > 0.02 and len(cols) > 1:
        cols[1] = round(cols[1] + left, 2)
    return cols, chosen


def _fin_col_widths(n_periods: int) -> list[float]:
    """财务表列宽照参考件：项目列 6.61，最新期 3.61（「2026年3月末」一行放得下），
    更早的整年列 2.4；超过 4 列时整体压缩，保证不出版心。"""
    n = max(n_periods + 1, 2)
    if n == 2:
        return [6.61, 7.5]
    if n == 3:
        return [6.61, 3.9, 3.9]
    if n == 4:
        return [6.61, 3.61, 2.4, 2.4]
    widths = [5.6, 3.4] + [2.0] * (n - 2)
    total = sum(widths)
    return [round(w * TEXT_WIDTH_CM / total, 2) for w in widths]


def render_report(data: dict[str, Any], out_path: str | Path) -> Path:
    doc = Document()
    # 页面：A4，左右 3.17cm，上下 2.54cm
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21), Cm(29.7)
    sec.left_margin = sec.right_margin = Cm(3.17)
    sec.top_margin = sec.bottom_margin = Cm(2.54)

    # 版式写进样式层（Normal + docDefaults + 去网格），
    # 光给每个段落设直接格式是不够的：用户一编辑、一粘贴就回到 Word 默认。
    _setup_styles(doc)

    # ── 版式：块与块之间一个空行，块内（加粗句 + 它的正文）不留空 ──
    #
    # 空行的位置只有一处定义，就是这里的 _gap()：每写完一个「块」调一次。
    # 段内不留空，是因为加粗总起句和它的正文本来就在同一个段落里（_add_lead_para），
    # 一旦在两者之间插空段，读起来就像话没说完就断了。
    gap_on = (data.get("layout") or {}).get("para_gap") is not False

    def _gap():
        if gap_on:
            _add_blank(doc)

    company = data.get("company_full_name", "")
    code = (data.get("internal_rating_code") or "").strip()
    _add_para(doc, f"{company}  主体：【{code or '待填'}】", bold=True, align=WD_ALIGN_PARAGRAPH.LEFT)
    _gap()

    _add_para(doc, "正面：", bold=True)
    for i, item in enumerate(data.get("positive") or [], 1):
        _add_para(doc, f"{i}）{_end_period(item)}")
    _gap()

    _add_para(doc, "关注：", bold=True)
    for i, item in enumerate(data.get("concern") or [], 1):
        _add_para(doc, f"{i}）{_end_period(item)}")
    _gap()

    _add_para(doc, "1、基本情况", bold=True, align=WD_ALIGN_PARAGRAPH.LEFT)
    _add_para(doc, data.get("basic_info", ""))
    _gap()

    c = data.get("commentary") or {}

    # 「2、点评」下面第一段是外部条件，第二段才是总述。
    # 这一段不另立小节：它是点评的开场——外部条件先摆出来，紧接着才说这家公司
    # 在什么条件下赚了多少、从哪赚的。单独给一节，读者会当成与公司无关的综述。
    # 产业类写行业（不加加粗句），区域开发类写城市与财政（带加粗句）：
    # 稿子里有没有 lead 就是这两类的分水岭，不必再问主体类型。
    _add_para(doc, "2、点评", bold=True, align=WD_ALIGN_PARAGRAPH.LEFT)

    ind = c.get("industry") or {}
    if (ind.get("body") or "").strip():
        # 新稿的 industry 不带加粗句（它就是一段叙述）；旧稿若带 lead，照旧加粗显示
        if (ind.get("lead") or "").strip():
            _add_lead_para(doc, ind.get("lead", ""), ind.get("body", ""))
        else:
            _add_para(doc, ind.get("body", ""))
        _gap()

    ov = c.get("overview") or {}
    if ov:
        _add_lead_para(doc, ov.get("lead", ""), ov.get("body", ""))

    seg = c.get("segment_table")
    # 表被核掉时（材料没有分板块收入）rows 为空：不能把一张空表排进 Word，
    # 那会在正文里留下一道空白边框，读者以为是排版坏了。
    if seg and seg.get("rows"):
        # 总述段与业务构成表是两个块，中间要有空行（原来这里漏了，
        # 表就顶在总述段的最后一行上）
        _gap()
        # 表前引导语是一句正文（「2023—2025年及2026年1—6月，公司收入、占比及毛利率情况如下：」），
        # 按正文排版，不做成居中的表题。
        if seg.get("caption"):
            cap = _add_para(doc, seg["caption"])
            cap.paragraph_format.keep_with_next = True
        if seg.get("periods") and seg.get("sub_cols"):
            groups = [{"label": p, "cols": list(seg["sub_cols"])} for p in seg["periods"]]
            _add_table(
                doc,
                seg.get("sub_cols") or ["营业收入", "收入占比"],
                seg.get("rows") or [],
                unit_line=seg.get("unit_line", "单位：亿元"),
                col_widths_cm=seg.get("col_widths_cm"),
                bold_rows=[len(seg.get("rows") or []) - 1],
                groups=groups,
                extra_cols=seg.get("extra_cols") or [],
                first_col="业务板块",
            )
        else:
            _add_table(
                doc,
                seg.get("header") or ["业务板块", "营业收入", "收入占比", "毛利率"],
                seg.get("rows") or [],
                unit_line=seg.get("unit_line", "单位：亿元"),
                col_widths_cm=seg.get("col_widths_cm") or [4.91, 3.91, 3.1, 3.1],
                bold_rows=[len(seg.get("rows") or []) - 1] if seg.get("last_row_bold", True) else [],
            )
    _gap()

    from generator import segment_heading  # 与网页预览、校验共用同一套编号口径

    for i, s in enumerate(c.get("segments") or [], 1):
        name = s.get("name", "")
        body = s.get("body") or s.get("text") or ""
        _add_para(doc, f"{segment_heading(i, name)}{body}")
        _gap()          # 一个板块就是一段，段与段之间同样一个空行
    if not c.get("segments"):
        pass

    pf = c.get("profit") or {}
    cs = c.get("cash") or {}
    # 「盈利与现金流」是一段：新稿只写 profit，老稿可能还带着 cash 段。
    # 在出口处接起来，Word 里就是一段——校验、页面、导出三处对同一份文本。
    # 加粗句只留一句：盈利那句已经说了获现时，现金流那句不再并进来。
    def _join(a, b):
        a, b = (a or "").strip(), (b or "").strip()
        if not a:
            return b
        if not b:
            return a
        return a + ("" if a[-1] in "。！？；：" else " ") + b

    pf_lead = str(pf.get("lead") or "").strip()
    cs_lead = "" if (pf_lead and ("获现" in pf_lead or "现金流" in pf_lead)) \
        else str(cs.get("lead") or "").strip()
    pf_lead = _join(pf_lead, cs_lead)
    pf_body = _join(str(pf.get("body") or ""), str(cs.get("body") or ""))
    if pf_body or pf_lead:
        # 位置在分部业务段之后、资产段之前，与网页上的顺序一致：
        # 先讲这一年赚没赚到钱，再讲资产压在哪儿，最后讲欠了多少。
        _add_lead_para(doc, pf_lead, pf_body)
        _gap()

    at = c.get("asset") or {}
    if at:
        _add_lead_para(doc, at.get("lead", ""), at.get("body", ""))
        _gap()

    ft = c.get("financial_table")
    if ft:
        cap = _add_para(doc, ft.get("caption", "公司主要财务数据和财务指标（合并口径）"),
                        bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)
        cap.paragraph_format.keep_with_next = True  # 表题与表格同页
        _add_table(
            doc,
            ["项目"] + list(ft.get("periods") or []),
            ft.get("rows") or [],
            unit_line=ft.get("unit_line", "单位：亿元"),
            col_widths_cm=ft.get("col_widths_cm") or _fin_col_widths(len(ft.get("periods") or [])),
        )
        _gap()

    db = c.get("debt") or {}
    if db:
        _add_lead_para(doc, db.get("lead", ""), db.get("body", ""))
        _gap()

    mk = c.get("market") or {}
    if mk.get("body"):
        # 二级市场定价：债务段之后、专题段之前。没有取到数据时这一段整体不出现，
        # 不写「未获取到市场价格」——那是作业过程，不是报告内容。
        _add_lead_para(doc, mk.get("lead", ""), mk.get("body", ""))
        _gap()

    topics = c.get("special_topics") or []
    for t in topics:
        if isinstance(t, str):
            _add_para(doc, t)
        else:
            _add_lead_para(doc, t.get("lead", ""), t.get("body", ""))
        _gap()

    # 结尾多余的空段去掉：Word 末尾挂一个空行，页数会多出一页空白
    while doc.paragraphs and not doc.paragraphs[-1].text.strip() and not doc.paragraphs[-1].runs:
        p = doc.paragraphs[-1]._p
        p.getparent().remove(p)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(out_path)
    return out_path


def render_skeleton(template_json: str | Path, out_path: str | Path) -> Path:
    """按模板 JSON 生成空白骨架 docx（槽位以 {{}} 占位），供人工套写。"""
    tpl = json.loads(Path(template_json).read_text(encoding="utf-8"))
    data = {
        "company_full_name": "{{企业全称}}",
        "internal_rating_code": "{{内部评级代码}}",
        "positive": ["{{正面一}}", "{{正面二}}", "{{正面三}}"],
        "concern": ["{{关注一}}", "{{关注二}}", "{{关注三}}"],
        "basic_info": "{{基本情况：成立时间与前身 → 主营与功能定位 → 注册资本与实收资本（不一致时说明差额原因）、股东及持股比例、实际控制人 → 增资到位、上市状态、并表子公司数量。（股权、评级、审计机构未变的，以及董监高更替，一律不写）}}",
        "commentary": {
            "industry": {
                "lead": "{{产业类留空；区域开发类（城投）写一句加粗总起句：区域财力处在什么状态、对平台回款意味着什么}}",
                "body": "{{点评第一段（接在「2、点评」下面直接开始）。产业类写行业：供需格局（产量或需求增速、产能利用率、需求端构成）→ 价格与成本（价格走势与幅度、原材料与能源成本）→ 政策（真正作用在这一行业上的那一条），只写行业、不出现公司。区域开发类写区域：地区生产总值与增速 → 一般公共预算收入与政府性基金收入 → 财政自给率、上级补助、债务余额或债务率 → 公司在这套收支里的位置（向谁结算、什么时候回款）。每个判断后跟一个可核实的数}}",
            },
            "overview": {
                "lead": "{{总述段加粗汇总句：主体定位 + 经营趋势 + 但书}}",
                "body": "{{钱从谁那里来 → 公司自身经营与盈利主线数据 → 收入构成概览，末句引出业务构成表。行业的量价与政策已在行业段写过，这里不重抄}}",
            },
            "segment_table": {
                "unit_line": "单位：亿元",
                "header": ["业务板块", "营业收入", "收入占比", "毛利率"],
                "rows": [
                    ["{{板块一}}", "{{}}", "{{}}", "{{}}"],
                    ["{{板块二}}", "{{}}", "{{}}", "{{}}"],
                    ["营业总收入", "{{}}", "100.00%", "{{}}"],
                ],
            },
            "segments": [
                {"name": "{{板块一}}", "body": "{{业务模式与产出能力 → 报告期产出量（计量单位按业务性质选取）→ 价格或费率与收入 → 成本结构与毛利率变动原因 → 项目或订单储备 → 这一块受什么制约（接在前面的经营事实上写，不另起一句「该板块风险在于」）}}"},
                {"name": "{{板块二}}", "body": "{{同上}}"},
            ],
            "asset": {
                "lead": "{{资产段加粗汇总句：规模 + 结构 + 质量 + 受限}}",
                "body": "{{总资产规模 → 前五大科目逐项：科目 金额（占资产总额 ××.××%），主要为×××，能否变现／是否质押／账龄 → 前五大合计占比 → 受限资产（金额、占比、主要为哪些）}}",
            },
            "financial_table": {
                "caption": "公司主要财务数据和财务指标（合并口径）",
                "unit_line": "单位：亿元",
                "periods": ["{{最新期末}}", "{{最近完整年度}}", "{{上一年度}}"],
                "rows": [
                    ["资产总额", "{{}}", "{{}}", "{{}}"],
                    ["负债总额", "{{}}", "{{}}", "{{}}"],
                    ["所有者权益", "{{}}", "{{}}", "{{}}"],
                    ["资产负债率", "{{}}", "{{}}", "{{}}"],
                    ["营业总收入", "{{}}", "{{}}", "{{}}"],
                    ["净利润", "{{}}", "{{}}", "{{}}"],
                    ["经营活动现金流量净额", "{{}}", "{{}}", "{{}}"],
                    ["短期债务", "{{}}", "{{}}", "{{}}"],
                    ["长期债务", "{{}}", "{{}}", "{{}}"],
                    ["全部债务", "{{}}", "{{}}", "{{}}"],
                    ["总资本化比率", "{{}}", "{{}}", "{{}}"],
                    ["营业毛利率", "{{}}", "{{}}", "{{}}"],
                    ["EBITDA", "{{}}", "{{}}", "{{}}"],
                    ["货币资金/短期债务（倍）", "{{}}", "{{}}", "{{}}"],
                ],
            },
            "profit": {
                "lead": "{{盈利与现金流段加粗汇总句：这一年利润被什么吃掉、经营获现够不够覆盖}}",
                "body": "{{营业收入与同比 → 量的变化、价的变化、结构变化 → 利润总额与净利润及同比 → 亏损或大幅下滑逐项归因：毛利变动（量/价/成本）→ 期间费用 → 减值损失（资产减值、信用减值各多少、提在哪项资产）→ 投资收益、政府补助、资产处置、所得税，每项带金额并按影响排序 → 哪些偶发、哪些持续 → 经营活动净现金流及同比（由正转负或由负转正要明说）、主要来源与去向 → 收现比与原因 → 投资与筹资净额 → 非受限货币资金/短债、经营净现金流/利息支出的覆盖 → 利润与获现的差额落在哪 → 缺口靠什么补、这一来源取决于什么}}",
            },
            "cash": {"lead": "", "body": ""},
            "debt": {
                "lead": "{{债务段加粗汇总句：规模趋势 + 期限结构 + 即期压力 + 备用流动性}}",
                "body": "{{负债总额与资产负债率 → 全部债务及长短债结构 → 有息债务融资构成 → 利息保障与经营获现 → 非受限货币资金/短债 → 授信与未使用额度}}",
            },
            "market": {
                "lead": "{{二级市场定价段加粗判断句：定价处在什么水平，往哪个方向走（不带数字）}}",
                "body": "{{数据区间与口径（中债估值、存续债逐券中位数、公募无担保非永续）→ 期末信用利差绝对值与区间高低点、极差 → 期初期末变动幅度与相对幅度 → 期末在区间内的位置 → 超额利差绝对值（相对同评级同类型是更紧还是更松）→ 日均绝对变动与单日最大变动 → 收口：位置，不预测}}",
            },
            "special_topics": [],
        },
    }
    return render_report(data, out_path)


if __name__ == "__main__":
    import sys

    base = Path(__file__).resolve().parent.parent
    tpl = base / "01_模板" / "内评简评报告通用模板_v1.1.json"
    out = base / "01_模板" / "内评简评报告通用模板_v1.1.docx"
    if len(sys.argv) > 1 and sys.argv[1] == "skeleton":
        print(render_skeleton(tpl, out))
    else:
        print("usage: docx_writer.py skeleton")
