"""从财汇 MCP 聚合「事实台账」。

输出一个 dict，既是网页上给用户核对的数据面板，也是喂给大模型的唯一数据源。
所有金额统一换算为亿元（财汇原始口径为万元），比率保留原始百分数。
"""

from __future__ import annotations

import re
from typing import Any

from mcp_client import CaihuiClient, MCPError

BASIC_INDICATORS = ["注册资本", "实收资本", "成立日期", "所属行业", "企业性质",
                    "法定代表人", "注册地址", "员工人数", "是否城投"]

FIN_INDICATORS = [
    "营业总收入", "营业成本", "销售毛利率", "净利润", "利润总额", "投资收益",
    "公允价值变动收益", "EBITDA", "EBITDA利息保障倍数",
    "总资产", "总负债", "所有者权益", "资产负债率",
    "短期债务", "长期债务", "有息债务", "货币资金", "应收账款", "存货",
    "固定资产", "在建工程合计", "经营活动产生的现金流量净额",
]

MONEY_FIELDS = {
    "营业总收入", "营业成本", "净利润", "利润总额", "投资收益", "公允价值变动收益",
    "EBITDA", "总资产", "总负债", "所有者权益", "短期债务", "长期债务", "有息债务",
    "货币资金", "应收账款", "存货", "固定资产", "在建工程合计", "经营现金流",
}

RATIO_FIELDS = {"销售毛利率", "资产负债率"}


def _wan_to_yi(v):
    try:
        return round(float(v) / 10000.0, 2)
    except (TypeError, ValueError):
        return None


def classify_material(title: str, category: str | None = None) -> str | None:
    t = (title or "") + " " + (category or "")
    if "跟踪评级" in t or "信用评级报告" in t or "评级报告" in t:
        return "跟踪评级报告"
    if "募集说明书" in t:
        return "募集说明书"
    if "半年度报告" in t or "中期报告" in t:
        return "半年报"
    if "年度报告" in t or "年报" in t:
        return "年度报告"
    if "季度" in t or "季报" in t:
        return "季度报告"
    if "审计" in t:
        return "审计报告"
    return None


def _pivot_basic(out: dict) -> dict[str, str]:
    rows = CaihuiClient.rows(out)
    res = {}
    for r in rows:
        k = (r.get("指标名") or "").strip()
        v = r.get("指标值")
        if not k:
            continue
        k = re.sub(r"\(.*?\)$", "", k)  # 去掉单位后缀，单位在值里已表达
        res[k] = v
    return res


def _norm_fin(rows: list[dict], periods_wanted: int = 4) -> dict[str, Any]:
    """把财汇长表（每期一行）整理为 {期次: {字段: 数值(亿元或%)}}。"""
    if not rows:
        return {"periods": [], "data": {}}
    by_period: dict[str, dict[str, Any]] = {}
    for r in rows:
        period = r.get("报告期") or ""
        item = {}
        for k, v in r.items():
            if k in ("公司名称", "统一社会信用代码", "报告期") or v is None:
                continue
            if k in RATIO_FIELDS:
                try:
                    item[k] = round(float(v), 2)
                except (TypeError, ValueError):
                    item[k] = None
            elif k in MONEY_FIELDS:
                item[k] = _wan_to_yi(v)
            else:
                try:
                    item[k] = round(float(v), 4)
                except (TypeError, ValueError):
                    item[k] = v
        by_period[period] = item
    periods = list(by_period.keys())[:periods_wanted]
    return {"periods": periods, "data": {p: by_period[p] for p in periods}}


def build_facts(client: CaihuiClient, company: str, with_financials: bool = True) -> dict:
    facts: dict[str, Any] = {"query_name": company, "company": {}, "equity": {},
                             "ratings": [], "bonds": [], "financials": {"periods": [], "data": {}},
                             "composition": [], "materials": {}, "notes": []}

    # 1) 基础档案
    try:
        out = client.execute("get_company_basic_info",
                             {"target_company": [company], "indicator_name": BASIC_INDICATORS})
        rows = CaihuiClient.rows(out)
        facts["company"]["name"] = rows[0].get("企业名称") if rows else company
        facts["company"]["credit_code"] = rows[0].get("统一社会信用代码") if rows else ""
        facts["company"]["indicators"] = _pivot_basic(out)
    except MCPError as e:
        facts["notes"].append(f"基础档案查询失败：{e}")

    # 2) 股权
    try:
        out = client.execute("get_company_actual_controller", {"target_company": [company]})
        rows = CaihuiClient.rows(out)
        if rows:
            facts["equity"]["controller"] = rows[0]
            facts["company"]["credit_code"] = (facts["company"].get("credit_code")
                                               or rows[0].get("统一社会信用代码"))
    except MCPError as e:
        facts["notes"].append(f"实际控制人查询失败：{e}")
    try:
        out = client.execute("get_company_direct_shareholders", {"target_company": [company]})
        facts["equity"]["shareholders"] = CaihuiClient.rows(out)
    except MCPError as e:
        facts["notes"].append(f"股东查询失败：{e}")

    # 3) 评级历史
    try:
        out = client.execute("get_corporate_credit_ratings", {"target_company": [company]})
        facts["ratings"] = CaihuiClient.rows(out)[:8]
    except MCPError as e:
        facts["notes"].append(f"评级历史查询失败：{e}")

    # 4) 存量债券
    try:
        out = client.execute("get_company_issued_bonds", {"target_company": [company]})
        facts["bonds"] = CaihuiClient.rows(out)
    except MCPError as e:
        facts["notes"].append(f"债券查询失败：{e}")

    # 5) 财务指标（分两次取：近三年年报 + 最新报告期，避免指标过多时接口截断）
    if with_financials:
        merged: dict[str, dict] = {}
        for date_arg in ("近三年年报", "最新报告期"):
            try:
                out = client.execute("get_company_financial_metrics", {
                    "target_company": [company],
                    "indicator_name": FIN_INDICATORS,
                    "date": date_arg,
                    "merge_type": "合并",
                })
            except MCPError:
                continue
            for period, item in _norm_fin(CaihuiClient.rows(out))["data"].items():
                if period:
                    merged.setdefault(period, {}).update({k: v for k, v in item.items() if v is not None})
        if merged:
            order = {"年报": 3, "中报": 2, "三季报": 1, "一季报": 0, "季报": 0}

            def sort_key(p: str):
                m = re.match(r"(\d{4})年(.+)", p or "")
                return (int(m.group(1)) if m else 0, order.get(m.group(2) if m else "", -1))

            periods = sorted(merged.keys(), key=sort_key, reverse=True)[:4]
            facts["financials"] = {"periods": periods, "data": {p: merged[p] for p in periods}}
        try:
            out = client.execute("get_company_business_composition",
                                 {"target_company": company, "date": "最新年报"})
            facts["composition"] = CaihuiClient.rows(out)
        except MCPError as e:
            facts["notes"].append(f"主营构成查询失败：{e}（改用评级报告原文分段）")

    # 6) 材料（公告）
    materials: dict[str, list[dict]] = {}
    queries = [
        ({"title_keywords": ["跟踪评级"], "limit": 30}, None),
        ({"title_keywords": ["评级报告"], "limit": 30}, None),
        ({"title_keywords": ["募集说明书"], "limit": 20}, None),
        ({"title_keywords": ["年度报告"], "limit": 20}, None),
    ]
    seen: set[str] = set()
    for args, _ in queries:
        try:
            out = client.execute("search_company_announcements",
                                 {"target_company": [company], **args})
        except MCPError:
            continue
        for r in CaihuiClient.rows(out):
            url = r.get("公告链接") or ""
            if not url or url in seen:
                continue
            kind = classify_material(r.get("公告标题", ""), r.get("公告分类"))
            if not kind:
                continue
            seen.add(url)
            materials.setdefault(kind, []).append({
                "title": (r.get("公告标题") or "").strip(),
                "date": r.get("公告日期") or "",
                "category": r.get("公告分类") or "",
                "source": r.get("公告来源") or "",
                "url": url,
            })
    for kind in materials:
        materials[kind].sort(key=lambda x: x["date"], reverse=True)
    facts["materials"] = materials
    return facts


def pick_materials(facts: dict, kinds: list[str] | None = None, per_kind: int = 1) -> list[dict]:
    """默认取：最新一份跟踪评级报告 + 最新一份募集说明书 + 最近一期年报。"""
    kinds = kinds or ["跟踪评级报告", "募集说明书", "年度报告"]
    picked = []
    for kind in kinds:
        picked.extend((facts.get("materials", {}).get(kind) or [])[:per_kind])
    return picked
