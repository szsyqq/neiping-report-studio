"""从财汇取「资料清单」。

省点数的关键：一次检索拿全，本地分类。
`search_company_announcements` 没有必传参数，不传关键词就按公告日期降序返回，
所以先用一次宽查询取回最近 60 条，在本地按标题分成评级报告与募集说明书；
只有某一类确实没出现时，才带关键词补查一次（同样只补缺的那类）。
最坏 3 次、常态 1 次，比原来固定两次 + 兜底一次省一半以上。

结构化台账暂时留空：报告的判断与数字改由材料原文支撑（评级报告里本来就有
「主要财务数据」表）。等需要结构化口径时，再按需把某个接口单独打开。
"""

from __future__ import annotations

import re
from typing import Any

from mcp_client import CaihuiClient, MCPError

# 网页上只展示这两类，也只允许下载这两类
MATERIAL_KINDS = ("跟踪评级报告", "募集说明书")

# 一次宽查询取回的公告条数；城投与产业债发行人的公告量都不大，
# 60 条足够覆盖近两年的评级报告与募集说明书
BULK_LIMIT = 60

PER_KIND = 12

# 分类缺失时的补查关键词（同一类只补一次）
FALLBACK_KEYWORDS = (
    ("跟踪评级报告", ["评级报告"]),
    ("募集说明书", ["募集说明书"]),
)


def empty_facts(company: str) -> dict[str, Any]:
    return {"query_name": company, "company": {"name": company}, "equity": {},
            "ratings": [], "bonds": [], "financials": {"periods": [], "data": {}},
            "composition": [], "materials": {}, "notes": []}


def classify_material(title: str, category: str | None = None) -> str | None:
    t = (title or "") + " " + (category or "")
    if "跟踪评级" in t or "信用评级报告" in t or "评级报告" in t:
        return "跟踪评级报告"
    if "募集说明书" in t:
        return "募集说明书"
    return None


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
            got = classify_material(r.get("公告标题", ""), r.get("公告分类"))
            if not got:
                continue
            seen.add(url)
            materials.setdefault(got, []).append({
                "title": (r.get("公告标题") or "").strip(),
                "date": r.get("公告日期") or "",
                "category": r.get("公告分类") or "",
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

    # 只补缺的那一类；两类都齐就到此为止（常态只花 1 点）
    for kind, kws in FALLBACK_KEYWORDS:
        if materials.get(kind):
            continue
        _p(progress, 55 if kind == "跟踪评级报告" else 70, "补充检索", f"未找到{kind}，按关键词补查")
        try:
            out = client.execute("search_company_announcements",
                                 {"target_company": [company],
                                  "title_keywords": kws, "limit": PER_KIND})
            rows = CaihuiClient.rows(out)
            queries.append(f"{kws[0]} {len(rows)} 条")
            absorb(rows)
        except MCPError as e:
            facts["notes"].append(f"「{kws[0]}」检索失败：{e}")

    for kind in materials:
        materials[kind].sort(key=lambda x: x["date"], reverse=True)
        materials[kind] = materials[kind][:PER_KIND]
    facts["materials"] = materials
    if stats is not None:
        stats["calls"] = client.calls
        stats["queries"] = queries
    _p(progress, 96, "整理清单",
       "、".join(f"{k} {len(v)} 份" for k, v in materials.items()) or "未检索到材料")
    return facts


def pick_materials(facts: dict, kinds: list[str] | None = None, per_kind: int = 1) -> list[dict]:
    """默认取：最新一份跟踪评级报告 + 最新一份募集说明书。"""
    kinds = kinds or list(MATERIAL_KINDS)
    picked = []
    for kind in kinds:
        picked.extend((facts.get("materials", {}).get(kind) or [])[:per_kind])
    return picked


def title_key(title: str) -> str:
    return re.sub(r"\.pdf$", "", (title or "").strip(), flags=re.I)
