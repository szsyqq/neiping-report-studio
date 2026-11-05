"""正文的局部改写与补写：选中一段换个说法，或在某一段后面补一层分析。

与整篇生成的区别只有一个字：**范围**。

整篇生成时模型看得全，所以它可以自己组织结构、自己找数字；局部改写只给它
一段上下文，它就不该有「补一个数」的余地——顺手编出来的数字没有任何出处，
比留白更糟。所以这里的第一条纪律是：**数字只能取自原文，需要新数而原文没有
就改写成定性表述**，并要求它把改动限制在所指的那个范围内。

第二个区别是产出形态：整篇生成给 JSON，这里给纯文本。局部改写要的是「能直接
替换掉那一段话」，多一层 JSON 只会多一次解析失败的機會。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import llm
import usage

# 动作只有一个字的差别，含义必须写死，不然模型会自己发挥：
# rewrite 保数字换说法，tighten 砍字不砍事实，expand 补机制不补数，
# plain 把评价改成可核实的事实。四个动作对应界面上四个按钮。
ACTIONS = {
    "rewrite": ("把这段话换个说法重写一遍", "意思、判断、数字都不变，只换表达"),
    "tighten": ("把这段话压缩", "删掉重复与铺垫，保留全部事实与数字，字数压缩到原来的七成左右"),
    "expand": ("把这段话写得更具体", "补上面这句话怎么发生的、在哪一环体现，不加原文之外的新数字"),
    "plain": ("把这段话里的评价改成事实", "去掉没有刻度的评价词与套话，换成可核实的事实或条件"),
}

_SYS = """你在改一份信用评级内部简评报告的正文，只改用户指出的那一处。

【铁律】
1. 数字只能取自给出的原文。原文里没有的数，一个都不许写；需要表达量级而原文没给数，就用定性表述（「明显下降」「略有上升」），不许补数、不许按比例推算。
2. 只改指定的那段话。前后文是给你看语境的，不要顺手改它们，也不要在回答里复述它们。
3. **不许增加原文里没有的事实**：不补「不存在质押」「未发生重大变化」这类否定式交代，也不补新的判断。原文说的是什么，改完还是什么。
4. 保持评级报告的语体：主语是公司、行业或政策，不用「我们」「本文」「材料」「披露」这类词，不用感叹与宣传语。
5. 判断词要分级（很强／较强／一般／较弱、充足／尚可／承压／不足）。不写没有刻度的词：显著、突出、良好、充裕、大幅、全面、有力。
6. 直接输出改好的那段文字，不要解释改了什么，不要加引号、不要加「修改后：」这类前缀。

【输出】
只输出一段中文正文，长度与目标相当。"""


def _record(kind: str, u: dict, seconds: float, *, company: str = "", note: str = "") -> None:
    """记一行用量。这里是一次几十秒的小调用，但它会连着点几次，
    不记账的话「今天花了多少」就对不上。"""
    try:
        usage.record(kind, company=company, model=u.get("model") or "",
                     prompt_tokens=int(u.get("prompt_tokens") or 0),
                     completion_tokens=int(u.get("completion_tokens") or 0),
                     total_tokens=int(u.get("total_tokens") or 0),
                     cached_tokens=int(u.get("cached_tokens") or 0),
                     estimated=bool(u.get("estimated")),
                     seconds=round(seconds, 1), note=note)
    except Exception:                                     # noqa: BLE001 — 记账失败不影响改稿
        pass


def _ctx(text: str, limit: int = 900) -> str:
    return (text or "").strip()[:limit]


def refine(*, text: str, action: str = "rewrite", hint: str = "",
           before: str = "", after: str = "", section: str = "", full: str = "",
           model_id: str | None = None) -> dict:
    """改写一段选中的文字，返回改后的那段（不是整段）。"""
    sel = (text or "").strip()
    if not sel:
        raise ValueError("没有选中文字")
    if len(sel) > 1200:
        raise ValueError("选中的内容太长，一次改一段话（1200 字以内）更稳")
    act, how = ACTIONS.get(action or "rewrite", ACTIONS["rewrite"])
    if (hint or "").strip():
        act, how = "按用户的要求改这段话", (hint or "").strip()[:400]
    parts = [f"【任务】{act}；{how}。"]
    if section:
        parts.append(f"【位置】{section}")
    if before:
        parts.append(f"【上文（只作语境，不要改）】{_ctx(before, 400)}")
    parts.append(f"【要改的这段】\n{sel}")
    if after:
        parts.append(f"【下文（只作语境，不要改）】{_ctx(after, 400)}")

    u: dict = {}
    t0 = time.time()
    out = llm.chat(
        [{"role": "system", "content": _SYS},
         {"role": "user", "content": "\n\n".join(parts)}],
        model_id=model_id, temperature=0.25, max_tokens=900, timeout=180,
        usage_out=u,
    )
    new = _tidy(out, sel)
    _record("refine", u, time.time() - t0, note=action or "rewrite")
    note = _diff_note(sel, new, ref=full)
    # 比对基准用整段原文，不只选中的那截：改写常把相邻分句一起重述，
    # 拿选中片段作基准，会把原文别处本来就有的数误报成「新出现的数」。
    warn = _guard(new, [full or sel, before, after])
    hg = _hedge(new)
    warn = "；".join(x for x in (warn, hg) if x)
    if warn:
        note += " · " + warn
    return {"before": sel, "after": new, "action": action, "note": note,
            "warn": warn}


def _new_numbers(text: str, sources: list[str]) -> list[str]:
    """生成文本里出现了、但原文与材料里都没有的数。

    提示词说不许编数，但它仍会漏一个。这一步是兜底：只要冒出一个新数，
    就在对照弹窗里标出来——人本来就要看一眼才决定要不要换上去，
    把该看的那一处指出来，比让他自己逐字核对省事。
    """
    def nums(t: str) -> set[str]:
        return set(re.findall(r"\d+(?:\.\d+)?", t or ""))

    have = set()
    for s in sources:
        have |= nums(s)
    return [n for n in sorted(nums(text), key=lambda x: -len(x)) if n not in have]


# 没有依据的否定式交代。提示词里已经禁过，但这类句子读起来最像「核查过了」，
# 模型最愿意写；程序兜一道，至少让人在确认时看到它。
_HEDGE = re.compile(r"未发生|不存在|未出现|无重大|未被|未涉及|未予|保持稳定|未受影响|未因")


def _hedge(text: str) -> str:
    hits = list(dict.fromkeys(_HEDGE.findall(text or "")))
    if not hits:
        return ""
    return "有「" + "」「".join(hits[:4]) + "」这类否定式交代——材料没提到不等于不存在，请确认有依据"


def _guard(text: str, sources: list[str]) -> str:
    """给前端的一句警告。没有新数就返回空串。"""
    bad = _new_numbers(text, sources)
    if not bad:
        return ""
    return "出现了原文之外的数字：" + "、".join(bad[:6]) + (" 等" if len(bad) > 6 else "") + "，请核对"


def _tidy(out: str, old: str) -> str:
    """去掉模型爱加的前缀与引号，再兜一次「换了引号、改了标点」这类无意义差异。"""
    s = (out or "").strip()
    s = re.sub(r"^(修改后|改后|改写后|答案|结果)\s*[：:]\s*", "", s)
    s = s.strip().strip("“”\"'「」")
    s = re.sub(r"\s*\n+\s*", "", s)          # 正文不分行
    if not s:
        return old
    return s


def _diff_note(old: str, new: str, ref: str = "") -> str:
    """一句人读的变化说明：字数怎么变、原句的数字还在不在。

    只说「数字有变动」会误导：改写常常把前后分句一起重述，新句里多出来的
    往往是原句本来就有的数。真正要看的是两件事——**原句的数有没有丢**、
    **有没有凭空多出原句没有的数**。
    """
    def nums(t: str) -> list[str]:
        return re.findall(r"\d+(?:\.\d+)?", t or "")

    a, b = nums(old), nums(new)
    sa, sb = set(a), set(b)
    # 「少了」比的是选中的那句；「多出来」比的是整段——改写常把相邻分句一起
    # 重述，新句里多出的数很可能就来自同一段的上一句，拿选区比会一路误报。
    base = set(nums(ref or old))
    lost = [x for x in a if x not in sb]
    added = [x for x in b if x not in base]
    bits = [f"{len((old or '').strip())} 字 → {len((new or '').strip())} 字"]
    if lost:
        bits.append("原句的数少了：" + "、".join(dict.fromkeys(lost))[:40])
    elif added:
        bits.append("新出现的数：" + "、".join(dict.fromkeys(added))[:40] + "（确认来源）")
    else:
        bits.append("数字未变")
    return " · ".join(bits)


# 用户点「补写一段」时给的主题。不是限制，只是把常写的那几项摆在手边——
# 让人从空白输入框开始想「该补什么」，比给他四个更贵的选项慢得多。
TOPICS = {
    "basic": ["股东与持股比例", "实际控制人与出资到位情况", "子公司与并表范围", "治理结构与董监高变动"],
    "industry": ["区域市场容量与竞争格局", "价格与成本的走势", "政策与补贴机制"],
    "overview": ["收入结构的变化", "盈利从哪里来", "对政府结算的依赖"],
    "asset": ["应收账款的质量与账龄", "存货构成与去化", "受限资产", "在建工程与投资缺口"],
    "profit": ["毛利率变动的原因", "期间费用的刚性", "减值计提的依据", "政府补助的依赖"],
    "debt": ["债务期限结构", "授信与未使用额度", "对外担保", "非标与明股实债"],
    "market": ["成交与估值水平", "利差与同评级比较"],
    "topics": ["对外担保", "未决诉讼", "股东注资进度", "在建项目资金缺口"],
}

_SYS_ADD = """你在给一份信用评级内部简评报告补一小段分析。

【铁律】
1. 只写被要求的那一个主题，不重复上下文已经说过的话。
2. 数字只能取自【材料摘录】或【现有正文】。找不到就用定性表述，绝不补数、绝不推算。
3. **每一句都要有落点**：这句话说的是材料摘录里哪一条，或者现有正文里已经交代过的哪件事。落不到任何一处的句子，删掉——不要用「对公司信用形成支撑」「整体风险可控」这类总结句补空。
4. **不许写没有依据的否定式交代**：材料没提到质押，不等于「股权不存在质押」；没提到诉讼，不等于「无重大未决诉讼」。这类话读者会当成核查结论，而你没有核过。
5. **不许补时间线与口径来源**：不要写「由设立时的×%调整为×%」这种原文没给的历史路径，也不要写「材料未予列明」「尚未披露」「无公开信息」——交代哪里有缺口属于作业过程，不是报告的语言。原文没给的那一层，直接不写。
6. 这一段是接在现有正文后面的**补充**：不写「此外」「值得一提的是」这类过渡语开头，直接进入事实与判断。
7. 语体同正文：主语是公司，判断词分级，不用宣传语与比喻，不写「详见」「如上所述」。
8. 长度 120—220 字，一段话写完，不分行、不加小标题、不加引号。

【输出】
只输出这一段的正文。"""


def expand(*, topic: str, section: str = "", section_text: str = "",
           draft_text: str = "", materials: list[dict] | None = None,
           model_id: str | None = None, company: str = "") -> dict:
    """就一个主题补一小段分析，接在指定段落后面。"""
    t = (topic or "").strip()
    if not t:
        raise ValueError("请说明要补写什么")
    digest = _digest(materials or [])
    parts = [f"【要补写的主题】{t}",
             f"【补在哪一节之后】{section or '正文'}"]
    if section_text:
        parts.append(f"【这一节现在的正文（不要重复它）】\n{_ctx(section_text, 1800)}")
    if draft_text:
        parts.append("【报告其他部分已说过的话（只是别重复，不是事实来源，"
                     "不要从这里取数字）】\n" + _ctx(draft_text, 1200))
    if digest:
        parts.append(f"【材料摘录】\n{digest}")
    u: dict = {}
    t0 = time.time()
    out = llm.chat(
        [{"role": "system", "content": _SYS_ADD},
         {"role": "user", "content": "\n\n".join(parts)}],
        model_id=model_id, temperature=0.3, max_tokens=800, timeout=240,
        usage_out=u,
    )
    text = _tidy(out, "")
    _record("expand", u, time.time() - t0, company=company, note=t[:40])
    # 补写要比改写更小心：它能拿到的材料只有压缩摘录，摘录之外的部分
    # 它也「觉得」自己知道。冒出来的新数一律标出来。
    warn = "；".join(x for x in (_guard(text, [section_text, digest]),
                                 _hedge(text)) if x)
    return {"topic": t, "text": text, "section": section, "warn": warn,
            "note": f"{len(text)} 字" + (" · " + warn if warn else "")}


def _digest(materials: list[dict], budget: int = 9000) -> str:
    """材料的压缩摘录：按份平均分配字符，够模型核对数字即可。

    不整份塞进来：局部补写只要「这个公司有没有这个数」，给三万字材料
    只会让它跑到别的话题上去，也拖慢一次几十秒的调用。
    """
    if not materials:
        return ""
    each = max(1200, budget // max(1, len(materials)))
    out = []
    for m in materials[:6]:
        t = (m.get("text") or "").strip()
        if t:
            out.append(f"— {m.get('title') or ''} —\n{t[:each]}")
    return "\n\n".join(out)[:budget]


def draft_brief(draft: dict) -> str:
    """整篇的要点，**只用来避免补写的内容与别处重复**。

    只给各段的加粗判断句，不给正文数字：给了数字，模型就会顺手把别段的数
    搬到这一段来（「补持股情况」里冒出「同期净利润 -2.18 亿元」就是这么来的）。
    判断句本身不带数字，正好够它判断「这句话是不是已经说过了」。
    """
    if not isinstance(draft, dict):
        return ""
    c = draft.get("commentary") or {}
    bits = []
    for key, name in (("industry", "行业"), ("overview", "总述"), ("asset", "资产"),
                      ("profit", "盈利与现金流"), ("debt", "债务"), ("market", "二级市场")):
        lead = str((c.get(key) or {}).get("lead") or "").strip()
        if lead:
            bits.append(f"{name}：{lead}")
    segs = [f"板块「{s.get('name')}」" for s in (c.get("segments") or []) if s.get("name")]
    if segs:
        bits.append("分部业务已写：" + "、".join(segs))
    topics = [str(t.get("lead") or "").strip() for t in (c.get("special_topics") or [])]
    topics = [x for x in topics if x]
    if topics:
        bits.append("专题段：" + "；".join(topics))
    return "\n".join(bits)


if __name__ == "__main__":       # 手动试跑：python3 editor.py "要改的句子"
    import sys
    print(json.dumps(refine(text=sys.argv[1] if len(sys.argv) > 1 else "公司收入下降。"),
                     ensure_ascii=False, indent=1))
