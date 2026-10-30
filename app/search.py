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

默认走 DeepSeek 自研搜索（`/v1/tools/search`）：与模型同一个 key，不必另申请账号，
每 1000 次成功请求 5 元，无结果不计费。博查／Serper／Tavily 保留可选。
"""

from __future__ import annotations

import difflib
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


def _deepseek(q: str, cfg: dict) -> list[dict]:
    """DeepSeek 自研搜索。与模型共用 key，计费按请求次数而非条数。

    summary 是 600–800 字的长摘要、正好够模型读；snippet 只有 100–200 字，
    给人扫一眼够用、写进正文往往太薄。这里两个都取：正文用 summary，
    界面上仍显示 snippet 以免一条占满半屏。
    """
    out = _post(cfg["url"], {"query": q, "count": cfg["max_results"],
                             "need_content": False}, cfg["apiKey"])
    res = []
    for r in (out.get("results") or []):
        # 官方把 1.5 以上叫「优质」，但那是通用查询的口径：按「某公司 某事项」这种
        # 具体长尾词查，相关结果通常只有 0.8–1.0，卡 1.5 会一条不剩。这里只在
        # 明显离题（<0.6）时丢弃，剩下的交给人工勾选。
        sc = r.get("rerank_score")
        if sc is not None and float(sc) < 0.6:
            continue
        res.append({"title": _clean(r.get("title"), 120),
                    "url": r.get("url") or "",
                    "site": _clean(r.get("site_name") or "", 40),
                    "date": (r.get("date") or "")[:10],
                    "snippet": _clean(r.get("snippet"), 320),
                    "detail": _clean(r.get("summary") or r.get("snippet"), 900),
                    "authority": r.get("authority")})
    return res


_ADAPTERS = {"deepseek": _deepseek, "bocha": _bocha,
             "serper": _serper, "tavily": _tavily}


def _norm_title(t: str) -> str:
    return re.sub(r"[\s　:：()（）\[\]【】<>《》\-—_、,，.。]", "", t or "")


def _dup_of_known(title: str, known: list[str]) -> bool:
    """这条结果是不是手里已有的材料。

    不是洁癖——「XX公司 2026 跟踪评级报告」这种查询，前几条必然是我们刚读完的
    那份报告。把它列出来让人勾，等于用外网搜了一遍自己的硬盘。
    """
    t = _norm_title(title)
    if not t:
        return False
    for k in known:
        kk = _norm_title(k)
        if not kk:
            continue
        if t == kk or t in kk or kk in t:
            return True
        # 标题改写过的（「2025年度X报告」vs「X 2025 年跟踪评级报告」）比值判
        if difflib.SequenceMatcher(None, t, kk).ratio() >= 0.72:
            return True
    return False


def search(query: str, known_titles: list[str] | None = None) -> list[dict]:
    """搜一次。没配 key 直接返回空——调用方不必先判断。

    known_titles：当前已读材料的标题。与它们重复的结果直接丢掉。
    """
    cfg = settings_store.search_config()
    if not cfg["ready"]:
        return []
    fn = _ADAPTERS.get(cfg["provider"]) or _deepseek
    try:
        rows = fn(query, cfg)
    except SearchError:
        raise
    except Exception as e:                                     # noqa: BLE001
        raise SearchError(f"搜索结果解析失败：{e}") from e
    known = [x for x in (known_titles or []) if x]
    out = [r for r in rows if r["title"] and (r["snippet"] or r["url"])]
    if known:
        out = [r for r in out if not _dup_of_known(r["title"], known)]
    return out


# 值得去外网追一句的风险事件词。命中还不够——正文里如果已经给了金额、
# 比例或期次，说明材料里本来就有，不必再搜；只有「提了一句但没下文」才值得追。
EVENT_WORDS = (
    ("评级下调", "评级下调 原因"),
    ("列入观察", "评级 列入观察名单"),
    ("诉讼", "诉讼 被执行"),
    ("仲裁", "仲裁 事项"),
    ("行政处罚", "行政处罚"),
    ("失信", "失信被执行人"),
    ("担保代偿", "担保代偿"),
    ("控制权变更", "控制权变更"),
    ("资产划出", "资产无偿划转"),
    ("非标意见", "审计 非标准意见"),
)

_AMT = re.compile(r"\d[\d,.]*\s*(?:亿|万|%|％|元|吨|万亿)")


def _sentences(text: str) -> list[str]:
    return [x.strip() for x in re.split(r"[。；\n]", text or "") if x.strip()]


def plan_queries(draft: dict, facts: dict, max_n: int = 3) -> list[dict]:
    """要搜什么：三处缺口，不是「字数不够」。

    ① 风险事件提了一句但没下文——正文说了有诉讼／代偿／评级下调，却没给
       金额、期次或结论。这类最该补，读者看到「存在一定诉讼」只会更疑惑。
    ② 行业段没有可核实的数——既无近两年数字也无来源，说明全靠描述。
    ③ 段落确实薄——不足 120 字，连判断都没铺开。
    只给 3 个点：搜得多不是本事，把不相干的东西塞进正文才是麻烦。
    """
    name = (draft.get("company_full_name")
            or facts.get("company", {}).get("name") or "").strip()
    if not name:
        return []
    c = draft.get("commentary") or {}
    body_all = "\n".join(
        [(c.get("industry") or {}).get("body") or "",
         (c.get("overview") or {}).get("body") or "",
         (c.get("asset") or {}).get("body") or "",
         (c.get("profit") or {}).get("body") or "",
         (c.get("debt") or {}).get("body") or ""]
        + [(x.get("body") or "") for x in (c.get("segments") or [])])
    out: list[dict] = []

    # ① 风险事件只提了名字
    for kw, suffix in EVENT_WORDS:
        hit = next((s for s in _sentences(body_all) if kw in s), "")
        if not hit:
            continue
        if _AMT.search(hit):
            continue                      # 已有金额或比例，材料里有，不必搜
        out.append({"where": f"关注事项（{kw}）",
                    "seg": 0,
                    "query": f"{name} {suffix} 2026",
                    "why": "正文提到但未给金额、期次或进展"})

    # ② 行业段无可核实数据
    ind = ((c.get("industry") or {}).get("body") or "").strip()
    if ind and not _AMT.search(ind):
        out.append({"where": "行业情况段", "seg": 0,
                    "query": f"{name[:6]} 所属行业 2026 产量 价格 增速",
                    "why": "行业段没有可核实的数字"})

    # ③ 段落偏薄
    for i, seg in enumerate(c.get("segments") or [], 1):
        body = (seg.get("body") or "").strip()
        nm = (seg.get("name") or "").strip()
        if nm and 0 < len(body) < 120:
            out.append({"where": f"分部业务段 {i}）{nm}", "seg": i,
                        "query": f"{name} {nm} 2026", "why": f"该段只有 {len(body)} 字"})

    # 行业段整段缺失时也提示一次：不是「薄」，是「没有」
    if not ind:
        out.append({"where": "行业情况段", "seg": 0,
                    "query": f"{name[:6]} 所属行业 2026 行情",
                    "why": "行业段为空"})
    return out[:max_n]
