"""从财汇取「资料清单」。

只调用公告检索：跟踪评级报告一次、募集说明书一次，共两次接口。
不再逐个拉工商档案、股权、评级历史、存量债券、财务指标、主营构成——
那些接口单次返回体大，串起来十余次调用，等待时间长，下游提示词也跟着膨胀。

结构化台账暂时留空：报告的判断与数字改由材料原文支撑（评级报告里本来就有
「主要财务数据」表）。等需要结构化口径时，再按需把某个接口单独打开。
"""

from __future__ import annotations

import re
from typing import Any

from mcp_client import CaihuiClient, MCPError

# 网页上只展示这两类，也只允许下载这两类
MATERIAL_KINDS = ("跟踪评级报告", "募集说明书")

# 检索计划：(标题关键词, 归入的材料类型)
SEARCHES = (
    ("跟踪评级", "跟踪评级报告"),
    ("募集说明书", "募集说明书"),
)

PER_KIND = 12


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


def collect_materials(client: CaihuiClient, company: str, progress=None) -> dict:
    """两次公告检索，返回带 materials 的台账骨架。"""
    facts = empty_facts(company)
    materials: dict[str, list[dict]] = {}
    seen: set[str] = set()

    plans = list(SEARCHES)
    for i, (keyword, kind) in enumerate(plans):
        _p(progress, 15 + i * 40, "公告检索", f"检索「{keyword}」")
        try:
            out = client.execute("search_company_announcements", {
                "target_company": [company],
                "title_keywords": [keyword],
                "limit": PER_KIND,
            })
            rows = CaihuiClient.rows(out)
        except MCPError as e:
            facts["notes"].append(f"「{keyword}」检索失败：{e}")
            rows = []

        # 跟踪评级查不到时（无存续债或未做跟踪评级），退一步用「评级报告」
        if not rows and keyword == "跟踪评级":
            try:
                out = client.execute("search_company_announcements", {
                    "target_company": [company],
                    "title_keywords": ["评级报告"],
                    "limit": PER_KIND,
                })
                rows = CaihuiClient.rows(out)
                facts["notes"].append("未检索到跟踪评级报告，已改用「评级报告」")
            except MCPError as e:
                facts["notes"].append(f"「评级报告」检索失败：{e}")

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

    for kind in materials:
        materials[kind].sort(key=lambda x: x["date"], reverse=True)
        materials[kind] = materials[kind][:PER_KIND]
    facts["materials"] = materials
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
