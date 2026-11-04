"""二级市场定价：从财汇取主体利差，整理成一段可写进正文的数据块。

只整理，不解释——和 analysis 一样，这里不说「说明市场认可度提升」这类话，
判断留给写正文的人（模型）。

口径说明（写进正文时必须照此交代）：
    估值与利差都是中债估值口径，按主体存续债逐券计算后取中位数；
    信用利差(较国开债) = 单券中债估值收益率 − 同期限中债国开债收益率（插值）；
    超额利差 = 单券估值收益率 − 同期限、同隐含评级、同债券类型的基准曲线收益率。
中债估值收益率本身常常没有权限（返回空），但利差有值，写正文用利差即可。
"""

from __future__ import annotations

import re
import threading
from datetime import date, datetime

CACHE: dict[str, dict] = {}


def _f(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x


def _pick(row: dict, *names: str):
    """按中文列名取值；财汇的列名偶有空格或后缀差异，逐个试。"""
    for n in names:
        if n in row and row[n] not in (None, ""):
            return row[n]
    for k, v in row.items():
        kk = re.sub(r"\s+", "", str(k))
        if any(re.sub(r"\s+", "", n) in kk for n in names) and v not in (None, ""):
            return v
    return None


def fetch_spread(client, company: str, days: int = 40, wait: float | None = None) -> dict:
    """取该主体近一个月的利差序列。取不到就返回 {}，不抛错、不写「未披露」。

    wait：愿意为这份数据等多久（秒）。给 None 就一直等。生成正文时给一个上限——
    定价数据是锦上添花，而财汇偶有几分钟才回（连接要过隧道），一次生成不该
    为了这一段卡住；等不到就先不写这一段，后台那趟跑完下一次即可命中缓存。
    按「主体 + 日期」缓存，同一天只问一次财汇。
    """
    key = re.sub(r"\s+", "", company or "")
    if not key:
        return {}
    hit = CACHE.get(key)
    if hit and hit.get("day") == date.today().isoformat():
        return hit.get("data") or {}

    if wait is None:
        return _fetch_now(client, company, days, key)

    box: dict = {}

    def job():
        box["data"] = _fetch_now(client, company, days, key)

    th = threading.Thread(target=job, daemon=True)
    th.start()
    th.join(max(0.5, float(wait)))
    return box.get("data") or {}


def _fetch_now(client, company: str, days: int, key: str) -> dict:
    try:
        out = client.execute("get_company_spread", {
            "target_company": [company],
            "date": "近一个月",
            "limit": days,
        })
        rows = client.rows(out)
    except Exception:                                      # noqa: BLE001 — 没配 / 没权限 / 超时
        return {}

    recs: list[dict] = []
    for r in rows:
        d = _pick(r, "日期")
        if not d:
            continue
        recs.append({
            "date": str(d)[:10],
            "cdb": _f(_pick(r, "信用利差(较国开债)", "信用利差（较国开债）")),
            "cgb": _f(_pick(r, "信用利差(较国债)", "信用利差（较国债）")),
            "excess": _f(_pick(r, "超额利差")),
            "yield": _f(_pick(r, "估值收益率")),
        })
    recs = [x for x in recs if x["cdb"] is not None or x["cgb"] is not None]
    if not recs:
        return {}
    recs.sort(key=lambda x: x["date"])

    def span(field: str) -> dict:
        vals = [(x["date"], x[field]) for x in recs if x.get(field) is not None]
        if not vals:
            return {}
        first, last = vals[0][1], vals[-1][1]
        return {"first": first, "last": last, "change": round(last - first, 2),
                "min": min(v for _, v in vals), "max": max(v for _, v in vals)}

    data = {
        "day": date.today().isoformat(),
        "company": company,
        "from": recs[0]["date"],
        "to": recs[-1]["date"],
        "n": len(recs),
        "cdb": span("cdb"),
        "cgb": span("cgb"),
        "excess": span("excess"),
        "series": [{"date": x["date"], "cdb": x["cdb"], "cgb": x["cgb"]} for x in recs],
    }
    CACHE[key] = {"day": data["day"], "data": data}
    return data


def warm(company: str) -> None:
    """后台预热：读完材料就把定价取回来，等真按下生成时直接命中缓存。

    放在读材料之后而不是生成之前，是因为财汇这一支偶有几秒到几分钟不等的
    「第一次连接」开销——让它在人挑材料、写判断的那几分钟里跑完就好。
    """
    try:
        from mcp_client import CaihuiClient
        fetch_spread(CaihuiClient(timeout=40), company)
    except Exception:                                      # noqa: BLE001
        pass


def as_text(data: dict) -> str:
    """给模型的材料块。人读也通顺，正文里的期次与幅度直接取这里的数。"""
    if not data or data.get("n") in (None, 0):
        return ""
    lines = [
        f"【二级市场定价】{data['company']}",
        f"数据区间：{data['from']} 至 {data['to']}，共 {data['n']} 个交易日；"
        "中债估值口径，按主体存续债逐券计算后取中位数（公募、无担保、非永续、优先行权再到期）",
    ]
    for field, label in (("cdb", "信用利差（较国开债）"), ("cgb", "信用利差（较国债）")):
        s = data.get(field) or {}
        if not s:
            continue
        trend = "走阔" if s["change"] > 0 else ("收窄" if s["change"] < 0 else "持平")
        lines.append(
            f"{label}：{s['first']:.2f} BP（{data['from']}）→ {s['last']:.2f} BP（{data['to']}），"
            f"{trend} {abs(s['change']):.2f} BP；区间 {s['min']:.2f} ~ {s['max']:.2f} BP")
    # 超额利差长期在 0 附近：这个数本身就是「与同评级同类型基准相当」，
    # 按走势写法会变成「0.00 → 0.00 持平」，读起来像数据没取到。
    ex = data.get("excess") or {}
    if ex:
        dev = max(abs(ex.get("min") or 0.0), abs(ex.get("max") or 0.0))
        if dev < 1.0:
            lines.append(f"超额利差：区间内偏离不超过 {dev:.2f} BP，基本为零——"
                         "即定价与同评级、同类型、同期限的基准相当（不必报走势，也不必报区间）")
        else:
            trend = "走阔" if ex["change"] > 0 else ("收窄" if ex["change"] < 0 else "持平")
            lines.append(
                f"超额利差：{ex['first']:.2f} BP → {ex['last']:.2f} BP，{trend} "
                f"{abs(ex['change']):.2f} BP；区间 {ex['min']:.2f} ~ {ex['max']:.2f} BP")
    tail = data.get("series") or []
    if tail:
        recent = tail[-8:]
        lines.append("最近 8 个交易日信用利差（较国开债，BP）："
                     + "、".join(f"{x['date'][5:]} {x['cdb']:.2f}"
                                 for x in recent if x.get("cdb") is not None))
    lines.append("说明：以上均为中债估值口径的市场定价，不是成交价；"
                 "中债估值收益率本文不使用，只使用利差与走势。")
    return "\n".join(lines)


def describe(data: dict) -> dict:
    """给前端与接口的精简结构（不含逐日序列以外的冗余）。"""
    if not data:
        return {"ready": False}
    return {
        "ready": True,
        "company": data.get("company") or "",
        "from": data.get("from") or "",
        "to": data.get("to") or "",
        "n": data.get("n") or 0,
        "cdb": data.get("cdb") or {},
        "cgb": data.get("cgb") or {},
        "excess": data.get("excess") or {},
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
