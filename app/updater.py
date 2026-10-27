"""评级更新：在既有内评简评报告上做最小必要修订。

与「新建」的区别只在两处，其余（检索、材料、待核对、导出）共用同一套：
- 输入多一份旧报告 docx，它是唯一修改底稿；
- 产出不是全新正文，而是一份 Action List，交由 Word 原生修订标记落地。

为什么不重写整篇：评级报告改的是「哪一处该改成什么」，逐条改动才可被审阅、
可被拒绝。整篇重写看着省事，实际上把「这次到底改了什么」这件事丢掉了。

方法论来自 skill `rating-report-update`（用户维护的 12 轮扫描与四类动作），
本文件只做工程化：把旧报告切成带编号的段落、把规则压成模型能执行的清单、
把模型给的 original_text 与实际段落对齐（对不齐就自动纠偏或标失败，不猜）。
"""

from __future__ import annotations

import difflib
import json
import re
from pathlib import Path

import llm
import revision


# ---------------------------------------------------------------- 文档解析

def _iter_block_paragraphs(doc):
    """按文档顺序取正文段落（含表格内段落），跳过空段。

    parent 必须传 doc 本身而不是 body 元素：python-docx 的段落对象要靠
    parent.part 解析样式，传元素会在取 style 时抛 AttributeError。
    """
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    def walk(container):
        for child in container.iterchildren():
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "p":
                p = Paragraph(child, doc)
                if p.text.strip():
                    yield p, False
            elif tag == "tbl":
                table = Table(child, doc)
                for row in table.rows:
                    for cell in row.cells:
                        for p in cell.paragraphs:
                            if p.text.strip():
                                yield p, True

    yield from walk(doc.element.body)


def load_docx(path: str | Path) -> dict:
    """读旧报告：全文、带编号的段落清单、表格数。

    paragraphs 里保留 python-docx 的段落对象，供后续精确定位使用；
    serial 是给人看的编号，模型引用 original_text 时不必依赖它，
    但定位失败时用它回指「大概是哪一段」。
    """
    from docx import Document

    doc = Document(str(path))
    rows: list[dict] = []
    for i, (p, in_table) in enumerate(_iter_block_paragraphs(doc), 1):
        rows.append({
            "serial": f"P{i:03d}",
            "text": p.text,
            "in_table": in_table,
            "style": (p.style.name if p.style is not None else "") or "",
            "bold": bool(p.runs and all(r.bold for r in p.runs if r.text.strip())),
        })
    text = "\n".join(r["text"] for r in rows)
    return {
        "doc": doc,
        "rows": rows,
        "text": text,
        "chars": len(text),
        "tables": sum(1 for r in rows if r["in_table"]),
        "path": str(path),
    }


def outline(rows: list[dict], limit: int = 400) -> str:
    """给模型看的旧报告视图：编号 + 段落文本。

    编号的用处不是让模型去引用它（引用靠逐字复制），而是让「这段在文档里的
    位置」有个共同说法：预检失败时能说「第 P023 段附近」，人和模型都对得上。
    """
    lines: list[str] = []
    for r in rows[:limit]:
        tag = "〔表〕" if r["in_table"] else ""
        lead = "〔加粗句〕" if r["bold"] else ""
        lines.append(f"[{r['serial']}]{tag}{lead} {r['text']}")
    if len(rows) > limit:
        lines.append(f"……另有 {len(rows) - limit} 段未列出")
    return "\n".join(lines)


# ---------------------------------------------------------------- 提示词

UPDATE_SYSTEM_PROMPT = """你是信用评级机构的分析员。任务是在一份既有的内评简评报告上做「最小必要修订」，不是为了体现更新而重写。你的产出是一份 Action List，程序会据此在 Word 里生成原生修订标记，供人逐条接受或拒绝。

【铁律】
1. 旧报告是唯一修改底稿。不得改变它的标题体系、章节顺序、表格行列结构；不得按外部报告的结构重排。每条改动都要能回答：旧报告的哪一处，要改成什么。
2. 只有可比才允许确定性修改：同一主体、同一口径（合并／母公司）、同一指标定义、同一报告期含义。口径不明或不可比，一律给 REVIEW，不要猜。
3. 不得因为外部材料更晚、更权威就判定旧报告错了。历史年份的事实与当期数据是两件事，年份不同、内容相似不等于旧报告有误。
4. 旧报告没错，不等于不用更新。外部材料使原判断深化、强化、弱化、或补出缓释因素时，即使不矛盾也要评估 REPLACE／INSERT。
5. 只做与信用判断有关的改动。外部评级机构自身的方法论、评级观点、ESG 泛论、免责声明不进正文。

【必须走完的检查，不要找到几处就收手】
1. 逐项核对事实与数据：年份、报告期、金额、比例、数量、排名、股权比例、主体与项目名称、经营与财务指标、表格数值。小变化也要看。
2. 表格结构比对：除数值外，检查行／列是否变化（新增子公司单列、合并行、表头含义变化）。结构变化要出 Action：标题或单元格用 REPLACE，新增行用 INSERT（new_text 以「|」分隔单元格），删除行用 DELETE。
   注意：表格里只能插「行」，不能插句子。要新增一句正文，锚点必须取一个**正文段落**（带〔表〕标记的是表格单元格，不要拿它当句子锚点）。
3. 项目状态逐项核对：旧改、在建、代建等项目，除金额外必须核对状态描述（前期拆迁→二期施工、模式由旧转新）。状态变了、金额没变也要 REPLACE。
4. 定性判断与趋势：外部材料是否改变、强化或弱化旧报告关于行业阶段、经营趋势、风险程度、市场地位、债务与流动性压力的判断。
5. 摘要与正文一致性：正文新事实是否要同步影响正面／关注／总起句／表格；同一事实在多处出现时是否都覆盖。
6. 单位与口径：外部底稿多用万元列示，核对前先定位原始表格表头单位再换算。不得只按「亿元」检索就断言外部未披露。自算数值必须重算并做数量级检查。
7. DELETE 从严：仅当业务剥离、事项终止、原表述明确有误、或最新事实证明原判断不成立时才删。「外部未提及」不等于「旧内容失效」。
8. INSERT 从严：必须同时满足——对信用判断有实质影响、是新事实或新风险、旧报告完全未覆盖、能自然纳入现有章节、不改整体框架。
9. 反向扫描：旧报告没有、但影响当前信用分析的重要新事项（重大业务或股权变化、新增风险、流动性与资本支出压力、重大诉讼担保违约）→ INSERT；拿不准 → REVIEW。
   旧报告若没有单列「或有事项」一节，而外部材料给出了对外担保余额、占净资产比重或诉讼涉案金额，要主动给 INSERT：original_text 取债务段最后一句，new_text 以独立的判断句开头（先说结论、不带数字）再摆数据，并在 reason 里写明「建议在债务段之后新增『或有事项』一节」——补不补由人定，但位置要让看的人一眼知道。

【动作四类】
- REPLACE：事实纠正、数据与年份更新、业务状态变化、趋势或风险程度变化、判断调整、补充新支撑或缓释因素。
- INSERT：旧报告没有、对信用判断有实质影响的重要新事项。**original_text 必须填「插在哪一句之后」，逐字复制那一句原话**——程序靠它定位，留空就插不进去；location 只是给人看的说明，不能替代它。
- DELETE：有充分证据证明已失效、已终止或原本错误。
- REVIEW：重要差异但无法安全自动修改，需人工判断。new_text 留空，在 reason 里写清「差在哪里、要人定什么」。

【粒度】
- 数值类（金额、比例、年份、期次、数量，含千分位与百分号）必须整体替换：original_text 与 new_text 都要包含完整数值，不要只给变化的尾数。程序会自动做整值替换。
- 文字类按最小必要片段给：只把要改的那一句写进 original_text 与 new_text，不动的地方不要抄进来。
- 一段里有若干独立问题，分别出多条 Action，不要合成一条大改写。
- 不要把整段大改写当作省事的选择。只有当新旧句高度相似却在语义上反转、逐词 diff 读不通时，才在该条加 "mode": "whole"。

【original_text 的写法（最容易出错，务必逐字）】
- 从【旧报告】里原样复制，连标点、括号、空格、全半角都保持一致，不要顺手改错别字、不要补空格、不要把「，」改成「,」。
- 只取一个自然段内的一到两句，不跨段。
- 长度以能唯一定位为准：太短会命中多处，太长容易抄错。一般 15–80 字。
- 拿不准就取更短的一句。程序定位不到会记为失败，不会替猜。

【摘要层（正面／关注）怎么写】
- 沿用旧报告的写法：旧报告该条是纯定性的，刷新后仍须纯定性，不得因为外部恰好有数据就把金额、占比塞进摘要层；数据下沉到正文。
- 结论没有变化，就不要出 Action。不要为了体现更新去改造摘要。
- 例外只限「这个数字本身就是判断核心，删掉这条就不成立」（如资产负债率畸高）。判据是：删掉这个数，这条还成不成立。
- 新增摘要条目，只限旧报告完全未覆盖、对信用判断有实质影响的事项，且同样按定性写。

【输出】只输出一个 JSON 对象，不要解释文字：
{
  "actions": [
    {
      "action_id": "A001",
      "action": "REPLACE",
      "location": "旧报告里的章节位置，如「四、点评 第二段」",
      "original_text": "从旧报告逐字复制的一到两句",
      "new_text": "改后的文字；DELETE 与 REVIEW 留空",
      "reason": "为什么改这一处（一句话，说清是数据更新、状态变化还是判断强化）",
      "evidence": "外部材料里的依据原文或数据，尽量带期次",
      "source": "依据出处，如「跟踪评级报告·经营分析」"
    }
  ],
  "review_summary": {"replace_count": 0, "insert_count": 0, "delete_count": 0,
                     "review_count": 0, "coverage_check": "PASS"}
}"""


def build_update_prompt(old: dict, materials: list[dict], facts: dict | None = None) -> str:
    mat_block = "\n\n".join(
        f"===== {m.get('title', '材料')}（{m.get('date', '')}）=====\n{m.get('text', '')}"
        for m in materials if m.get("text"))
    fin: list[str] = []
    data = ((facts or {}).get("financials") or {}).get("data") or {}
    if data:
        fin.append("【台账财务数据（亿元/%，已换算，口径见备注）】")
        fin.append(json.dumps(data, ensure_ascii=False)[:4000])
    periods = ""
    if (facts or {}).get("financials"):
        try:
            import generator as _g
            periods = _g.period_labels(facts)
        except Exception:                                          # noqa: BLE001
            periods = ""
    return f"""请在下面这份旧报告上做修订，产出 Action List。

【旧报告】共 {len(old['rows'])} 段、{old['chars']} 字。段落前的 [P001] 是编号，仅供你说明位置，不要写进 original_text。
{outline(old['rows'])}

{chr(10).join(fin)}
{f"【期次写法】{periods}" if periods else ""}

【外部材料】
{mat_block}

要求：
1. 每条 original_text 必须是旧报告里的原文，逐字复制。
2. 先完成全部检查（含表格结构、项目状态、单位换算），再统一输出；不要找到几处就交。
3. 摘要层保持旧报告的定性写法，结论没变就不出 Action。
4. review_summary 里的计数要与 actions 实际条数一致，coverage_check 写 PASS 或 FAIL。
"""


# ---------------------------------------------------------------- 分析

def _p(progress, pct, stage, detail=""):
    if progress:
        progress(pct, stage, detail)


def analyze(old: dict, materials: list[dict], model_id: str | None = None,
            progress=None, facts: dict | None = None) -> dict:
    """调模型产出 Action List，并做定位预检。"""
    import threading
    import time

    _p(progress, 4, "组装提示词", "旧报告 + 外部材料")
    messages = [
        {"role": "system", "content": UPDATE_SYSTEM_PROMPT},
        {"role": "user", "content": build_update_prompt(old, materials, facts)},
    ]
    chars = old["chars"] + sum(len(m.get("text") or "") for m in materials)
    _p(progress, 10, "模型比对中", f"已送入约 {chars // 1000}k 字")

    stop = threading.Event()

    def tick():
        t0 = time.time()
        while not stop.wait(1.5):
            e = time.time() - t0
            pct = 10 + int(78 * (1 - 2.718281828 ** (-e / 70)))
            _p(progress, pct, "模型比对中", f"已用时 {int(e)} 秒")

    th = threading.Thread(target=tick, daemon=True)
    th.start()
    t0 = time.time()
    used: dict = {}
    try:
        raw = llm.chat(messages, model_id=model_id, temperature=0.2,
                       max_tokens=16000, usage_out=used)
    finally:
        stop.set()
        th.join(timeout=1)

    _p(progress, 90, "解析结果", "核对每条能否在旧报告里定位")
    payload = llm.parse_json(raw)
    actions = payload.get("actions") if isinstance(payload, dict) else payload
    if not isinstance(actions, list):
        raise ValueError("模型没有返回 actions 数组")

    checked = check_actions(actions, old)
    ok = sum(1 for a in checked if a["status"] != "failed")
    _p(progress, 97, "解析结果", f"{len(checked)} 条，可定位 {ok} 条")

    summary = (payload.get("review_summary") if isinstance(payload, dict) else None) or {}
    counts = {"REPLACE": 0, "INSERT": 0, "DELETE": 0, "REVIEW": 0}
    for a in checked:
        k = (a.get("action") or "").upper()
        if k in counts:
            counts[k] += 1
    elapsed = round(time.time() - t0, 1)
    return {
        "mode": "update",
        "actions": checked,
        "review_summary": {
            "replace_count": counts["REPLACE"], "insert_count": counts["INSERT"],
            "delete_count": counts["DELETE"], "review_count": counts["REVIEW"],
            "coverage_check": summary.get("coverage_check") or "",
            "failed_count": sum(1 for a in checked if a["status"] == "failed"),
        },
        "base": {"chars": old["chars"], "paras": len(old["rows"]),
                 "tables": old["tables"], "path": old["path"]},
        "usage": {**used, "seconds": elapsed},
        "validation": build_validation(checked),
    }


# ---------------------------------------------------------------- 定位预检

def _norm(s: str) -> str:
    """比对用的归一化：去空白、全半角标点统一。只用于找候选，不用于改写。"""
    t = (s or "").replace("\u3000", " ")
    table = {ord("，"): ",", ord("。"): ".", ord("；"): ";", ord("："): ":",
             ord("（"): "(", ord("）"): ")", ord("、"): ",", ord(" "): "",
             ord("\u201c"): '"', ord("\u201d"): '"', ord("\u2018"): "'",
             ord("\u2019"): "'"}
    return (t.translate(table)).strip()


def _best_para(old: dict, text: str) -> tuple[dict | None, float]:
    """在旧报告里找与 text 最像的段落，返回 (段落, 相似度)。"""
    want = _norm(text)
    if not want:
        return None, 0.0
    best, best_score = None, 0.0
    for r in old["rows"]:
        have = _norm(r["text"])
        if not have:
            continue
        if want in have:
            return r, 1.0
        # 用公共片段长度 / 目标长度衡量：比 SequenceMatcher 在全段上更快且更稳
        m = difflib.SequenceMatcher(None, want, have).ratio()
        if m > best_score:
            best, best_score = r, m
    return best, best_score


def check_actions(actions: list[dict], old: dict) -> list[dict]:
    """逐条核对 original_text 能否在旧报告里定位。

    三档：
    - exact  逐字命中，直接可用；
    - fixed  只是标点、空格、繁简差异，自动改成段落原文——这种偏差改了不改
             语义，自动纠正比让人逐条返工划算；
    - failed 找不到足够相似的段落，标出来让人看，绝不替猜。
    """
    out: list[dict] = []
    used: dict[str, int] = {}
    for i, a in enumerate(actions, 1):
        act = dict(a or {})
        act["action"] = (act.get("action") or "").upper()
        act["action_id"] = act.get("action_id") or f"A{i:03d}"
        orig = (act.get("original_text") or "").strip()
        act["status"] = "ok"
        act["hit"] = ""
        act["serial"] = ""

        if act["action"] == "REVIEW" or not orig:
            if act["action"] == "INSERT":
                # 新增条目没有锚点等于「不知道插在哪」，执行器只能跳过；
                # 与其让它静默失败，不如提前标出来让人补一句原话。
                act["status"] = "failed"
                act["note"] = ("新增条目没写「插在哪一句之后」，无法定位。"
                               "把要插入位置的前一句原话填进 original_text。")
            else:
                act["status"] = "review" if act["action"] == "REVIEW" else "ok"
            out.append(act)
            continue

        # 1) 逐字命中（含「段内子串」两种，与执行器一致）
        hit = next((r for r in old["rows"] if r["text"].strip() == orig), None)
        if hit is None:
            hit = next((r for r in old["rows"] if orig in r["text"]), None)
        if hit is not None:
            loc = revision.locate_paragraph(old["doc"], orig)
            if loc is not None:
                act["status"] = "exact"
                act["hit"] = orig
                act["serial"] = hit["serial"]
                # 表格里只能插行：锚点落在单元格、而 new_text 又不是行格式时，
                # 执行器会跳过。这种失败到导出才暴露就太晚了，先标出来。
                if (act["action"] == "INSERT" and hit.get("in_table")
                        and "|" not in (act.get("new_text") or "")):
                    act["status"] = "failed"
                    act["note"] = ("锚点落在表格单元格里，而表格只能插入新的行。"
                                   "请改用一个正文段落作锚点；若确要加表格行，"
                                   "new_text 用「|」分隔单元格。")
                used[hit["serial"]] = used.get(hit["serial"], 0) + 1
                out.append(act)
                continue

        # 2) 近似命中：标点／空格／全半角差异，改成段落原文
        cand, score = _best_para(old, orig)
        if cand is not None and score >= 0.86:
            fixed = _align_span(cand["text"], orig)
            loc = revision.locate_paragraph(old["doc"], fixed)
            if loc is not None:
                act["status"] = "fixed"
                act["original_raw"] = orig
                act["original_text"] = fixed
                act["hit"] = fixed
                act["serial"] = cand["serial"]
                act["note"] = "原文与旧报告有标点或空白差异，已按旧报告原文校正"
                out.append(act)
                continue

        # 3) 定位失败
        act["status"] = "failed"
        if cand is not None and score >= 0.45:
            act["near"] = {"serial": cand["serial"], "score": round(score, 2),
                           "text": cand["text"][:120]}
            act["note"] = (f"在旧报告里没找到这句原话，最接近的是"
                           f"{cand['serial']}（相似度 {score:.0%}）")
            act["serial"] = cand["serial"]
        else:
            act["note"] = "在旧报告里没找到这句原话，需人工指定位置"
        out.append(act)

    # 同一句被两条「会改变它」的动作命中，才是真冲突：前一条改完，
    # 后一条就找不到原文了。INSERT 不算——它只是借这句定位，插在这句之后，
    # 原句还在（执行时也排在替换之前）。
    seen: dict[str, list[str]] = {}
    for a in out:
        key = (a.get("original_text") or "").strip()
        if not key or a["status"] == "failed" or a["action"] == "INSERT":
            continue
        seen.setdefault(key, []).append(a["action_id"])
    for key, ids in seen.items():
        if len(ids) < 2:
            continue
        for a in out:
            if a["action_id"] not in ids:
                continue
            others = [x for x in ids if x != a["action_id"]]
            tail = f"；同一句还有 {'、'.join(others)}"
            a["note"] = (a.get("note") or "") + (
                f"这处原文被 {len(ids)} 条替换或删除同时引用{tail}，执行时只能落实前面那条。"
                "建议并成一条，或改用更长的定位文本区分。")
            if a["status"] == "exact":
                a["status"] = "conflict"
    return out


def _align_span(para_text: str, want: str) -> str:
    """把 want 对齐到 para_text 里的实际片段：从 para_text 中截出与 want 最匹配的一段。"""
    if want in para_text:
        return want
    n = len(want)
    if not n:
        return want
    best, best_score = want, 0.0
    # 以 want 的长度为窗，在段落上滑一遍，取相似度最高的窗口
    step = max(1, n // 6)
    for start in range(0, max(1, len(para_text) - n + 1), step):
        window = para_text[start:start + n]
        score = difflib.SequenceMatcher(None, _norm(window), _norm(want)).ratio()
        if score > best_score:
            best, best_score = window, score
    if best_score >= 0.9:
        return best
    # 退一步：取 want 在段落里最长的公共片段（首尾对齐）
    sm = difflib.SequenceMatcher(None, _norm(want), _norm(para_text))
    blocks = [b for b in sm.get_matching_blocks() if b.size >= 6]
    if not blocks:
        return want
    lo = blocks[0].b
    hi = blocks[-1].b + blocks[-1].size
    # 归一化后的下标与原文不一致，改用原始子串定位：找 want 首尾若干字在段落里的位置
    head = want[:8]
    tail = want[-8:]
    i = para_text.find(head)
    j = para_text.find(tail)
    if i >= 0 and j >= i:
        return para_text[i:j + len(tail)]
    return para_text[lo:hi] if lo < hi <= len(para_text) else want


# ---------------------------------------------------------------- 校验

def build_validation(actions: list[dict]) -> dict:
    """把预检结果整理成与新建模式同构的待核对结构，前端沿用同一套侧栏。

    分组按「要不要动手」排：定位失败必须先解决，REVIEW 要人给判断，
    条数失衡（全是 REPLACE 却零 REVIEW）提示可能漏扫。
    """
    issues: list[str] = []
    items: list[dict] = []

    failed = [a for a in actions if a["status"] == "failed"]
    if failed:
        issues.append(f"{len(failed)} 条改动的原文在旧报告里定位不到，"
                      "执行时会被跳过；请核对是否抄错，或改用更短的一句原文。")
        items.append({
            "t": "原文定位不到", "a": "blk-actions",
            "d": [{"num": a["action_id"], "where": a.get("location") or "",
                   "kind": a["action"], "full": (a.get("original_text") or "")[:160],
                   "si": 0, "seg": 0, "anchor": "blk-actions", "field": "act",
                   "note": a.get("note") or ""} for a in failed]})

    conflict = [a for a in actions if a["status"] == "conflict"]
    if conflict:
        issues.append(f"{len(conflict)} 处原文被多条替换或删除同时引用，"
                      "执行时只有前一条能落实；请并成一条或改用更长的定位文本。")
        items.append({
            "t": "同一处被改了两次", "a": "blk-actions",
            "d": [{"num": a["action_id"], "where": a.get("location") or "",
                   "kind": a["action"], "full": (a.get("original_text") or "")[:160],
                   "si": 0, "seg": 0, "anchor": "blk-actions", "field": "act",
                   "note": a.get("note") or ""} for a in conflict]})

    fixed = [a for a in actions if a["status"] == "fixed"]
    if fixed:
        issues.append(f"{len(fixed)} 条的原文与旧报告只在标点或空格上有差异，"
                      "已按旧报告原文校正后执行，建议扫一眼。")
        items.append({
            "t": "原文已按旧报告校正", "a": "blk-actions",
            "d": [{"num": a["action_id"], "where": a.get("location") or "",
                   "kind": a["action"], "full": (a.get("original_text") or "")[:160],
                   "si": 0, "seg": 0, "anchor": "blk-actions", "field": "act",
                   "note": a.get("note") or ""} for a in fixed]})

    review = [a for a in actions if a["action"] == "REVIEW"]
    if review:
        issues.append(f"{len(review)} 条标记为待人工判断，执行时保持原文；"
                      "交付说明里要单独列出。")
        items.append({
            "t": "需人工判断", "a": "blk-actions",
            "d": [{"num": a["action_id"], "where": a.get("location") or "",
                   "kind": "REVIEW", "full": (a.get("reason") or "")[:160],
                   "si": 0, "seg": 0, "anchor": "blk-actions", "field": "act",
                   "note": (a.get("evidence") or "")[:80]} for a in review]})

    # 条数结构：全篇只有 REPLACE 而没有 REVIEW，往往意味着没做可比性判断
    kinds = {k: sum(1 for a in actions if a["action"] == k)
             for k in ("REPLACE", "INSERT", "DELETE", "REVIEW")}
    if actions and kinds["REVIEW"] == 0 and kinds["REPLACE"] + kinds["INSERT"] > 5:
        issues.append("本轮没有一条 REVIEW，但改动较多。可比性不明的地方应当留给人工判断，"
                      "请确认是否所有数据都能确定口径一致。")

    no_evidence = [a for a in actions
                   if a["action"] in ("REPLACE", "INSERT", "DELETE")
                   and not (a.get("evidence") or "").strip()]
    if no_evidence:
        issues.append(f"{len(no_evidence)} 条改动没写依据，"
                      "审阅时无法核对来源：" + "、".join(
                          a["action_id"] for a in no_evidence[:5]))

    # 摘要层混入数据：与新建模式同一条纪律
    digit = re.compile(r"\d")
    for a in actions:
        if a["action"] not in ("REPLACE", "INSERT"):
            continue
        loc = a.get("location") or ""
        if not re.search(r"正面|关注|摘要", loc):
            continue
        new = a.get("new_text") or ""
        if digit.search(new) and not digit.search(a.get("original_text") or ""):
            issues.append(f"{a['action_id']} 往摘要层加了数字（{loc}）。"
                          "摘要只作定性概括，数据下沉到正文。")

    body_chars = sum(len(a.get("new_text") or "") for a in actions)
    return {"issues": issues, "issue_items": items, "auto_checks": [],
            "auto_count": len(fixed), "body_chars": body_chars,
            "rev": 1, "mode": "update"}


# ---------------------------------------------------------------- 执行

def apply(actions: list[dict], base_path: str | Path, out_path: str | Path,
          author: str = "内评简评") -> dict:
    """把 Action List 落到 docx 上，保留 Word 原生修订标记。

    REVIEW 也要一起传进去：执行器会跳过它并记入 reviewed。提前在这里过滤，
    交付说明就少了「这几条要人定」的清单——那部分恰恰不能丢。
    """
    from docx import Document

    runnable = [a for a in actions if a["action"] in ("REPLACE", "INSERT", "DELETE")]
    if not runnable:
        raise ValueError("没有可执行的改动（只有待人工判断的条目）")
    doc = Document(str(base_path))
    rev = revision.Revision(author)
    # 执行顺序有依赖：INSERT 与 DELETE 都要靠原句还活着才能定位，
    # 而 REPLACE 会改掉那句话。同一处既被替换、又被当作插入锚点时，
    # 先替换就会让插入落空。所以先插、再删、最后替换。
    order = {"INSERT": 0, "DELETE": 1, "REPLACE": 2}
    ranked = sorted(actions, key=lambda a: order.get(a.get("action", ""), 3))
    applied, failed, reviewed = revision.apply_actions(doc, ranked, rev)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out))

    id2act = {a["action_id"]: a for a in actions}
    return {
        "output": str(out),
        "applied": applied,
        "failed": failed,
        "reviewed": reviewed,
        "counts": {"applied": len(applied), "failed": len(failed),
                   "reviewed": len(reviewed), "total": len(actions)},
        # 落盘失败的那几条把原因带出来，前端直接显示，不必再翻日志
        "failed_detail": [
            {"action_id": aid, "location": (id2act.get(aid) or {}).get("location", ""),
             "original": ((id2act.get(aid) or {}).get("original_text") or "")[:80],
             "note": (id2act.get(aid) or {}).get("note", "")}
            for aid in failed],
        "review_detail": [
            {"action_id": aid, "location": (id2act.get(aid) or {}).get("location", ""),
             "reason": ((id2act.get(aid) or {}).get("reason") or "")[:160]}
            for aid in reviewed],
    }
