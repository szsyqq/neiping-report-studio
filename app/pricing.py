"""模型与搜索的花费：按官方价目表算成人民币。

为什么单独一个模块而不是散在各处算：价格会变，而且分高峰/空闲、缓存命中/未命中
四档；散着算迟早出现「页面上一个数、台账里另一个数」。这里只有一处价目表。

**口径（2026-10-30 取自 DeepSeek 官方「模型 & 价格」页）**
    单位：元／百万 tokens
    模型            输入(命中)      输入(未命中)     输出
                   空闲 / 高峰     空闲 / 高峰      空闲 / 高峰
    deepseek-flash  0.02 / 0.04     1.0  / 2.0       4.0 / 8.0
    deepseek-v4-pro 0.15 / 0.30     4.5  / 9.0      13.5 / 27.0
    搜索            每 1000 次成功请求 5 元（即 0.005 元／次，无结果不计费）

高峰时段：北京时间周一至周五（不含法定节假日）9:00–12:00、14:00–18:00，
其余时段单价减半。**节假日没有内置日历**，按工作日近似——国庆、春节这类长假
期间会按高峰算，宁可高估，不要让人以为比实际便宜。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# 元／百万 tokens
PRICES = {
    "deepseek-flash": {
        "in_hit": (0.02, 0.04), "in_miss": (1.0, 2.0), "out": (4.0, 8.0),
        "label": "DeepSeek V4.1 Flash",
    },
    "deepseek-v4-pro": {
        "in_hit": (0.15, 0.30), "in_miss": (4.5, 9.0), "out": (13.5, 27.0),
        "label": "DeepSeek V4 Pro",
    },
}
# 旧名按新模型计价（官方说明：旧模型名仍可调用，按 Flash 价格计费）
ALIAS = {"deepseek-v4-flash": "deepseek-flash",
         "deepseek-v4-flash-vision-exp": "deepseek-flash",
         "deepseek-chat": "deepseek-flash",
         "deepseek-reasoner": "deepseek-flash"}

SEARCH_UNIT = 0.005          # 元／次成功请求

CST = timezone(timedelta(hours=8))


def _key(model: str) -> str | None:
    m = (model or "").strip().lower()
    if m in PRICES:
        return m
    if m in ALIAS:
        return ALIAS[m]
    # 「deepseek-flash-1017」这类带版本后缀的，按前缀归位
    for k in PRICES:
        if m.startswith(k):
            return k
    return None


def is_peak(at: datetime | None = None) -> bool:
    """现在是高峰时段吗（北京时间工作日 9–12、14–18）。"""
    t = (at or datetime.now(CST)).astimezone(CST)
    if t.weekday() >= 5:                       # 周六周日全天空闲
        return False
    hm = t.hour * 60 + t.minute
    return (9 * 60 <= hm < 12 * 60) or (14 * 60 <= hm < 18 * 60)


def cost(model: str, prompt_tokens: int = 0, completion_tokens: int = 0,
         cached_tokens: int = 0, at: datetime | None = None) -> float:
    """这一次调用花了多少钱（元）。未知模型返回 0，不猜。"""
    k = _key(model)
    if not k:
        return 0.0
    p = PRICES[k]
    i = 1 if is_peak(at) else 0
    hit = max(0, min(int(cached_tokens or 0), int(prompt_tokens or 0)))
    miss = max(0, int(prompt_tokens or 0) - hit)
    total = (hit * p["in_hit"][i] + miss * p["in_miss"][i]
             + int(completion_tokens or 0) * p["out"][i])
    return round(total / 1_000_000, 4)


def rate(model: str, at: datetime | None = None) -> dict:
    """给页面显示用的单价（元／百万 tokens，按当前时段）。"""
    k = _key(model)
    if not k:
        return {}
    p = PRICES[k]
    i = 1 if is_peak(at) else 0
    return {"model": k, "label": p["label"], "peak": bool(i),
            "in_hit": p["in_hit"][i], "in_miss": p["in_miss"][i], "out": p["out"][i],
            "search": SEARCH_UNIT, "based_on": "DeepSeek 官方价目表 2026-10-30"}


def estimate(model: str, prompt_tokens: int, out_tokens: int,
             at: datetime | None = None) -> float:
    """生成之前的预估。按「输入全部未命中缓存」算——宁可高估。

    缓存命中与否取决于上一次请求是否同前缀，我们控制不了；按未命中估，
    实际只会更便宜。
    """
    return cost(model, prompt_tokens=prompt_tokens,
                completion_tokens=out_tokens, cached_tokens=0, at=at)


def fmt(yuan: float) -> str:
    """金额显示：小于一分钱也留两位小数之外的一位，避免看起来像免费。"""
    y = float(yuan or 0)
    if y == 0:
        return "¥0"
    if y < 0.01:
        return f"¥{y:.4f}"
    if y < 1:
        return f"¥{y:.3f}"
    return f"¥{y:.2f}"
