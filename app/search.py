"""外网搜索：补材料里没有的东西。

用途只有一个——正文写完之后，发现某个板块只写了两三句、或者行业数据材料里
压根没有，去外网找一条能引用的补充。不参与主线判断：评级报告与审计报告仍是
唯一的数字来源，这里补的是「近半年发生了什么」「行业产量增速是多少」这类
外部机构在报告里带一句、但我们的材料里恰好缺的东西。

**三条约束，写在这里也写进提示词：**
1. 搜到的内容一律不当成材料原文用。它要能说清是谁在什么时候说的，正文里写成
   「据××协会 2026 年 3 月数据」，不能只写一个孤零零的数。
2. 只补薄板块。一份报告里该搜的点不超过 3 个——搜得多不是本事，把不相干的
   东西塞进正文才是麻烦。
3. 结果先给人看，勾了才写进正文。自动往正文里塞外部信息，是这类工具最容易
   出事的地方：读者以为那是评级报告里的数，其实来自一条公众号。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

import settings_store

# 各家的请求体与响应结构差别不小，适配层只做这一件事。
# 返回值统一成 {title, url, site, date, snippet}，其余字段丢掉。
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")


class SearchError(RuntimeError):
    pass


def available() -> bool:
    return settings_store.search_config()["ready"]


def _post(url: str, body: dict, key: str, header: str = "Authorization",
          prefix: str = "Bearer ") -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json", "User-Agent": UA,
                 header: f"{prefix}{key}"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:200]
        except Exception:                                      # noqa: BLE001
            pass
        raise SearchError(f"搜索接口返回 {e.code}：{detail}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise SearchError(f"搜索接口连不上：{e}") from e


def _clean(s: str, n: int = 220) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    s = re.sub(r"\s+", " ", s).strip()
    return s[:n]


def _bocha(q: str, cfg: dict) -> list[dict]:
    """博查。请求体字段按官方 web-search 接口，summary 取摘要而非全文。"""
    out = _post(cfg["url"], {"query": q, "summary": True, "count": cfg["max_results"],
                             "freshness": "oneYear"}, cfg["apiKey"])
    rows = ((out.get("data") or {}).get("webPages") or {}).get("value") or []
    res = []
    for r in rows:
        res.append({"title": _clean(r.get("name"), 120),
                    "url": r.get("url") or "",
                    "site": _clean(r.get("siteName") or r.get("site"), 40),
                    "date": (r.get("datePublished") or "")[:10],
                    "snippet": _clean(r.get("summary") or r.get("snippet"), 320)})
    return res


def _serper(q: str, cfg: dict) -> list[dict]:
    out = _post(cfg["url"], {"q": q, "num": cfg["max_results"], "hl": "zh-cn"},
                cfg["apiKey"], header="X-API-KEY", prefix="")
    res = []
    for r in (out.get("organic") or [])[: cfg["max_results"]]:
        res.append({"title": _clean(r.get("title"), 120), "url": r.get("link") or "",
                    "site": _clean(r.get("source") or ""), "date": _clean(r.get("date"), 10),
                    "snippet": _clean(r.get("snippet"), 320)})
    return res


def _tavily(q: str, cfg: dict) -> list[dict]:
    out = _post(cfg["url"], {"query": q, "max_results": cfg["max_results"],
                             "search_depth": "basic", "api_key": cfg["apiKey"]},
                cfg["apiKey"], header="Authorization", prefix="Bearer ")
    res = []
    for r in (out.get("results") or []):
        res.append({"title": _clean(r.get("title"), 120), "url": r.get("url") or "",
                    "site": "", "date": _clean(r.get("published_date"), 10),
                    "snippet": _clean(r.get("content"), 320)})
    return res


_ADAPTERS = {"bocha": _bocha, "serper": _serper, "tavily": _tavily}


def search(query: str) -> list[dict]:
    """搜一次。没配 key 直接返回空——调用方不必先判断。"""
    cfg = settings_store.search_config()
    if not cfg["ready"]:
        return []
    fn = _ADAPTERS.get(cfg["provider"]) or _bocha
    try:
        rows = fn(query, cfg)
    except SearchError:
        raise
    except Exception as e:                                     # noqa: BLE001
        raise SearchError(f"搜索结果解析失败：{e}") from e
    return [r for r in rows if r["title"] and (r["snippet"] or r["url"])]


def plan_queries(draft: dict, facts: dict, max_n: int = 3) -> list[dict]:
    """要搜什么。只针对「写得薄」的地方，不针对「没写」的地方。

    薄的标准是正文不足 120 字——与校验里「板块段内容偏薄」同一条尺子。
    一个公司名 + 板块名就够当查询词，多余的限定词会让结果变窄。
    """
    name = (draft.get("company_full_name")
            or facts.get("company", {}).get("name") or "").strip()
    if not name:
        return []
    c = draft.get("commentary") or {}
    out: list[dict] = []
    for i, seg in enumerate(c.get("segments") or [], 1):
        body = (seg.get("body") or "").strip()
        nm = (seg.get("name") or "").strip()
        if nm and 0 < len(body) < 120:
            out.append({"where": f"分部业务段 {i}）{nm}", "seg": i,
                        "query": f"{name} {nm} 2026", "why": f"该段只有 {len(body)} 字"})
    ind = (c.get("industry") or {}).get("body") or ""
    if len(ind.strip()) < 200:
        out.append({"where": "行业情况段", "seg": 0,
                    "query": f"{name} 所属行业 2026 产量 价格", "why": f"行业段只有 {len(ind.strip())} 字"})
    return out[:max_n]
