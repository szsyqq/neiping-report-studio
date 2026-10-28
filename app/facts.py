"""从财汇取「资料清单」。

省点数的关键：一次检索拿全，本地分类。
`search_company_announcements` 没有必传参数，不传关键词就按公告日期降序返回，
所以先用一次宽查询取回最近一批公告，在本地按标题与公告分类分成四类；
只有某一类确实没出现时，才带关键词补查一次（同样只补缺的那类）。
最坏 4 次、常态 1 次。

四类是：跟踪评级报告、募集说明书、审计报告、其他公告。前两类是判断主线，
审计报告是财务数字的最终出处（且带审计意见），其他公告是报告期内变化的
唯一来源——重大诉讼、担保代偿、股权变更这类事只出现在公告里，评级报告要
等到下一期才写。付息兑付、上市流通这类程序性公告全部滤掉：它们数量最多、
对偿债判断没有增量信息，留着只会把清单填满。

结构化台账暂时留空：报告的判断与数字改由材料原文支撑（评级报告里本来就有
「主要财务数据」表）。等需要结构化口径时，再按需把某个接口单独打开。
"""

from __future__ import annotations

import re
from typing import Any

from mcp_client import CaihuiClient, MCPError

# 网页上按此顺序分组展示，也只允许读取这四类
MATERIAL_KINDS = ("跟踪评级报告", "募集说明书", "审计报告", "其他公告")

# 一次宽查询取回的公告条数。四类要一起覆盖，60 条在公告密集的主体上不够
# （付息与发行类占了近半），放到 120 条；限值是条数不是点数，多取不额外计费。
BULK_LIMIT = 120

# 每类最多展示几条：前两类是主线材料，审计报告按年披露、三年足够，
# 其他公告是给人挑的清单，24 条覆盖近两年重要事项。
PER_KIND = {"跟踪评级报告": 12, "募集说明书": 12, "审计报告": 8, "其他公告": 24}
PER_KIND_DEFAULT = 12

# 分类缺失时的补查关键词（同一类只补一次）
FALLBACK_KEYWORDS = (
    ("跟踪评级报告", ["评级报告"]),
    ("募集说明书", ["募集说明书"]),
    ("审计报告", ["审计报告"]),
)

# 年报分类下的标题要再看一眼才算法定审计报告：同一分类里既有「××年度审计
# 报告」，也有「公司债券年度报告」这种不含财务报表的文本。
AUDIT_CATS = ("年报", "审计及评估机构报告")
AUDIT_TITLE_HINTS = ("审计报告", "经审计", "审计的合并", "财务报告", "财务报表")

# 程序性公告：付息兑付、上市流通、发行结果、发行公告（含申购说明、承诺函）。
# 这些公告只说明债券按计划走，判断偿债能力用不上，检索时直接丢掉。
PROCEDURAL_CATS = ("付息兑付", "上市流通", "发行结果", "发行公告", "申购说明")

# 其他公告内部排序的加分词：出现这些字样的，多半是报告期内真变化
MATTER_HINTS = ("诉讼", "仲裁", "担保", "代偿", "处罚", "监管", "警示", "违约",
                "展期", "重组", "重整", "收购", "股权", "控制权", "股东", "增持",
                "减持", "质押", "冻结", "失信", "退市", "停牌", "重大", "持有人会议",
                "会计政策", "审计机构", "资金占用", "关联交易", "资产处置", "对外投资")
# 定期报告虽不是审计报告，却是最新一期数据的出处（半年报、季报先于年报）
FIN_REPORT_HINTS = ("年度报告", "半年度报告", "季度报告", "财务报表", "定期报告")


def empty_facts(company: str) -> dict[str, Any]:
    return {"query_name": company, "company": {"name": company}, "equity": {},
            "ratings": [], "bonds": [], "financials": {"periods": [], "data": {}},
            "composition": [], "materials": {}, "notes": []}


def _is_audit(title: str, category: str | None) -> bool:
    if (category or "") not in AUDIT_CATS:
        return False
    return any(k in (title or "") for k in AUDIT_TITLE_HINTS)


def classify_material(title: str, category: str | None = None) -> str | None:
    """判定一份公告属于哪类材料；返回 None 表示不进清单。

    顺序有讲究：募集说明书的标题里常带「评级报告」相关字眼的情况不多，
    但「××年度审计报告」的公告分类也可能是「年报」，所以先认标题里最硬的
    两个专有名称，再判审计报告，最后兜到其他公告。
    """
    t = (title or "") + " " + (category or "")
    if "跟踪评级" in t or "信用评级报告" in t or "评级报告" in t:
        return "跟踪评级报告"
    if "募集说明书" in t:
        return "募集说明书"
    if _is_audit(title or "", category):
        return "审计报告"
    if (category or "") in PROCEDURAL_CATS:
        return None
    if "更名公告" in (title or ""):
        return None
    return "其他公告"


def _matter_score(title: str, category: str | None = None) -> int:
    """其他公告的重要性打分，只用于排序，不用于过滤。

    命中实质事项词 +3，定期报告 +2；同一分档内再按日期降序。不打的公告
    仍会出现在清单里（只是排在后面），因为「重不重要」最终由人判断。
    """
    t = (title or "") + " " + (category or "")
    s = 0
    if any(k in t for k in MATTER_HINTS):
        s += 3
    if any(k in t for k in FIN_REPORT_HINTS):
        s += 2
    return s


def _p(progress, pct: int, stage: str, detail: str = ""):
    if progress:
        progress(pct, stage, detail)


def collect_materials(client: CaihuiClient, company: str, progress=None,
                      stats: dict | None = None) -> dict:
    """一次宽查询 + 必要的补查，返回带 materials 的台账骨架。

    stats 回填 {"calls": 本次实际 MCP 调用次数, "queries": [...]}，供用量统计显示。
    """
    facts = empty_facts(company)
    materials: dict[str, list[dict]] = {}
    seen: set[str] = set()
    queries: list[str] = []

    def absorb(rows: list[dict]) -> int:
        added = 0
        for r in rows:
            url = r.get("公告链接") or ""
            if not url or url in seen:
                continue
            title = (r.get("公告标题") or "").strip()
            cat = r.get("公告分类") or ""
            got = classify_material(title, cat)
            if not got:
                continue
            seen.add(url)
            materials.setdefault(got, []).append({
                "title": title,
                "date": r.get("公告日期") or "",
                "category": cat,
                "kind": got,
                "score": _matter_score(title, cat),
                "source": r.get("公告来源") or "",
                "url": url,
            })
            added += 1
        return added

    _p(progress, 12, "公告检索", "一次取回最近公告")
    try:
        out = client.execute("search_company_announcements",
                             {"target_company": [company], "limit": BULK_LIMIT})
        rows = CaihuiClient.rows(out)
        queries.append(f"最近公告 {len(rows)} 条")
        absorb(rows)
    except MCPError as e:
        facts["notes"].append(f"公告检索失败：{e}")

    # 只补缺的那一类；四类都齐就到此为止（常态只花 1 点）
    for i, (kind, kws) in enumerate(FALLBACK_KEYWORDS):
        if materials.get(kind):
            continue
        _p(progress, 50 + i * 8, "补充检索", f"未找到{kind}，按关键词补查")
        try:
            out = client.execute("search_company_announcements",
                                 {"target_company": [company],
                                  "title_keywords": kws,
                                  "limit": PER_KIND.get(kind, PER_KIND_DEFAULT)})
            rows = CaihuiClient.rows(out)
            queries.append(f"{kws[0]} {len(rows)} 条")
            absorb(rows)
        except MCPError as e:
            facts["notes"].append(f"「{kws[0]}」检索失败：{e}")

    for kind in list(materials):
        arr = _dedupe(kind, materials[kind])
        if kind == "其他公告":
            # 其他公告先按重要性分档，档内按日期降序：实质事项在前，
            # 定期报告其次，程序性文本垫底。
            arr.sort(key=lambda x: (x.get("score") or 0, x["date"]), reverse=True)
        elif kind == "审计报告":
            arr.sort(key=lambda x: x["date"], reverse=True)
        else:
            arr.sort(key=lambda x: x["date"], reverse=True)
        materials[kind] = arr[:PER_KIND.get(kind, PER_KIND_DEFAULT)]
    if not materials.get("其他公告"):
        materials.pop("其他公告", None)
    facts["materials"] = materials
    if stats is not None:
        stats["calls"] = client.calls
        stats["queries"] = queries
    _p(progress, 96, "整理清单",
       "、".join(f"{k} {len(materials[k])} 份" for k in MATERIAL_KINDS if materials.get(k))
       or "未检索到材料")
    return facts


def _dedupe(kind: str, rows: list[dict]) -> list[dict]:
    """去掉同一份材料的多处重复发布。

    同一件事常被拆成多条：审计报告在「年报」与「审计及评估机构报告」两个分类下
    各发一次（「2025年审计报告」与「2025年度审计报告」）；公告经不同债券的渠道
    各发一次（「26香建03：××公告」与「××公告」）。这些内容相同，读一份就够，
    重复读只会白占摘录预算。判重键按类别取：
    - 审计报告：只认年份，同一年只留一条，优先留标题提到「合并」的那条（往往
      是含完整报表的版本）；
    - 其他公告：标题去掉「代码：」前缀与「-巨潮资讯」这类来源后缀后比对；
    - 评级报告与募集说明书：标题完全去空格后比对（期次不同的不会撞）。
    """
    out: list[dict] = []
    index: dict[str, dict] = {}
    for r in rows:
        title = r.get("title") or ""
        if kind == "审计报告":
            y = re.search(r"(20\d{2})", title)
            key = y.group(1) if y else title
        elif kind == "其他公告":
            key = re.sub(r"^[0-9A-Za-z\u4e00-\u9fa5]{2,10}[：:]\s*", "", title)
            key = re.sub(r"[-－]\s*(?:巨潮资讯|中国货币网|上海清算所|上交所|深交所)$", "", key)
            key = re.sub(r"\s+", "", key)
        else:
            key = re.sub(r"\s+", "", title)
        if key in index:
            prev = index[key]
            # 同一年两版审计报告：谁提到「合并」留谁
            if kind == "审计报告" and "合并" in title and "合并" not in (prev.get("title") or ""):
                out[out.index(prev)] = r
                index[key] = r
            continue
        index[key] = r
        out.append(r)
    return out


def is_periodic(title: str) -> bool:
    """这份公告是不是「定期报告 / 财务报表」——内容就是几张报表，不是叙事公告。

    它归在「其他公告」里（没有审计意见，不算审计报告），但摘录时要用财务页
    优先的老策略，不能按公告的关键词打分去挑页，否则挑出来全是附注里的
    会计政策说明，报表本身一页都留不下。
    """
    return any(k in (title or "") for k in FIN_REPORT_HINTS)


def pick_materials(facts: dict, kinds: list[str] | None = None, per_kind: int = 1) -> list[dict]:
    """没勾选时的默认选法：最新跟踪评级报告、最新募集说明书各一份。"""
    kinds = kinds or ["跟踪评级报告", "募集说明书"]
    picked = []
    for kind in kinds:
        picked.extend((facts.get("materials", {}).get(kind) or [])[:per_kind])
    return picked


def title_key(title: str) -> str:
    return re.sub(r"\.pdf$", "", (title or "").strip(), flags=re.I)
