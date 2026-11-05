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
# 正在取的主体：同一主体第二次调用不再发一次请求，而是等第一次的结果。
# 不加这一层，读材料时后台 warm 一次、生成时又起一次，两次都要过财汇隧道，
# 各自 8 秒超时——两边都等不到，最后一段行情都没有。
_INFLIGHT: dict[str, threading.Event] = {}
_INFLIGHT_LOCK = threading.Lock()


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

    # 已经有人在取同一主体：等他的结果，不重复请求
    with _INFLIGHT_LOCK:
        ev = _INFLIGHT.get(key)
        owner = ev is None
        if owner:
            ev = threading.Event()
            _INFLIGHT[key] = ev
    if not owner:
        ev.wait(0.5 if wait is None else max(0.5, float(wait)))
        got = CACHE.get(key)
        if got and got.get("day") == date.today().isoformat():
            return got.get("data") or {}
        if wait is not None:
            return {}                      # 别人还在跑，这次先不写这一段
        # wait=None 表示愿意一直等，那就自己再取一次（对方可能已经失败）
    if wait is None:
        try:
            return _fetch_now(client, company, days, key)
        finally:
            if owner:
                with _INFLIGHT_LOCK:
                    _INFLIGHT.pop(key, None)
                ev.set()

    box: dict = {}

    def job():
        try:
            box["data"] = _fetch_now(client, company, days, key)
        finally:
            if owner:
                with _INFLIGHT_LOCK:
                    _INFLIGHT.pop(key, None)
                ev.set()

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
        """一条曲线的完整描述：水平（期初期末、区间）、变化、位置与波动。

        评级报告看利差是两件事：绝对水平说明「这个价是高是低」，变化说明
        「往哪个方向走」。只报变动、不报水平，读者不知道 3BP 是从 80 走到 83，
        还是从 400 走到 403。波动（单日最大变动、日均绝对变动）则回答这个价是
        连续形成还是某一天跳出来的——同一个月同一幅度，跳一次的定价含义不同。
        """
        vals = [(x["date"], x[field]) for x in recs if x.get(field) is not None]
        if not vals:
            return {}
        first, last = vals[0][1], vals[-1][1]
        lo = min(v for _, v in vals)
        hi = max(v for _, v in vals)
        rng = round(hi - lo, 2)
        steps = [abs(vals[i][1] - vals[i - 1][1]) for i in range(1, len(vals))]
        # 有方向的单日最大变动：跳升与跳降对定价的含义不同，不给方向会被写错
        step_i = max(range(1, len(vals)), key=lambda i: abs(vals[i][1] - vals[i - 1][1]),
                     default=0)
        out = {
            "first": first, "last": last, "change": round(last - first, 2),
            "min": lo, "max": hi, "range": rng,
            "mean": round(sum(v for _, v in vals) / len(vals), 2),
            # 变动的相对幅度：3BP 在 80BP 的曲线上是 3.75%，在 400BP 上不到 1%
            "change_pct": round((last - first) / abs(first) * 100, 2) if first else None,
            # 期末落在区间内的百分位，0 = 区间最低点，100 = 最高点
            "pos": round((last - lo) / rng * 100, 1) if rng > 0 else None,
            "avg_abs_step": round(sum(steps) / len(steps), 2) if steps else None,
            "max_step": round(vals[step_i][1] - vals[step_i - 1][1], 2) if step_i else None,
            "max_step_date": vals[step_i][0] if step_i else "",
        }
        return out

    data = {
        "day": date.today().isoformat(),
        "company": company,
        "from": recs[0]["date"],
        "to": recs[-1]["date"],
        "n": len(recs),
        "cdb": span("cdb"),
        "cgb": span("cgb"),
        "excess": span("excess"),
        "series": [{"date": x["date"], "cdb": x["cdb"], "cgb": x["cgb"],
                    "excess": x["excess"]} for x in recs],
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
    """给模型的材料块。人读也通顺，正文里的期次与幅度直接取这里的数。

    每档都按「绝对水平 → 变动 → 位置 → 波动」四件给全：只给变动，模型写出来
    就是「走阔 3BP」，读者不知道这 3BP 站在什么水平上；只给高低点，又会被写成
    「波动较大」这种没有刻度的判断。
    """
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
        seg = (f"{label}：期末 {s['last']:.2f} BP；区间 {s['min']:.2f} ~ {s['max']:.2f} BP，"
               f"极差 {s['range']:.2f} BP；期初 {s['first']:.2f} BP → 期末 {s['last']:.2f} BP，"
               f"{trend} {abs(s['change']):.2f} BP")
        if s.get("change_pct") not in (None, 0):
            seg += f"（相当于期初水平的 {abs(s['change_pct']):.2f}%）"
        if s.get("pos") is not None:
            seg += f"；期末位于区间 {s['pos']:.0f}% 分位"
        lines.append(seg)
        if s.get("avg_abs_step") is not None:
            vol = f"波动：日均绝对变动 {s['avg_abs_step']:.2f} BP"
            if s.get("max_step") is not None and s.get("max_step_date"):
                vol += (f"，单日最大变动 {s['max_step']:+.2f} BP"
                        f"（{s['max_step_date'][5:]}）")
            lines.append(vol)
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
            side = "偏紧" if ex["last"] < 0 else "偏松"
            lines.append(
                f"超额利差：期末 {ex['last']:.2f} BP、区间均值 {ex['mean']:.2f} BP，"
                f"相对同评级同类型基准{side}；期初 {ex['first']:.2f} BP → 期末 "
                f"{ex['last']:.2f} BP，{trend} {abs(ex['change']):.2f} BP；"
                f"区间 {ex['min']:.2f} ~ {ex['max']:.2f} BP")
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
    """给前端与接口的精简结构（含逐日序列，前端据此画走势图）。"""
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
        # 图例要能切换看哪条曲线，就得把逐日点一起给前端；一天最多几十个点，
        # 不构成负担，但省掉第二轮请求
        "series": data.get("series") or [],
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
