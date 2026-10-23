"""大模型调用层。

直接复用 WorkBuddy 已配置的模型（~/.workbuddy/models.json），
不需要另外填 API Key；调用走 OpenAI 兼容的 /chat/completions。
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

MODELS_JSON = Path.home() / ".workbuddy" / "models.json"

# 适合长文写作的偏好顺序（上下文大、中文好、性价比高）
PREFERRED = ["deepseek-v4", "kimi-k2", "glm-5.1", "qwen3.7-plus", "qwen3.7-max", "glm-5"]

# 最近一次 chat() 的 token 用量（进程级，便于排查）。
# 业务统计不要读它——用 chat(..., usage_out=本调用自己的 dict)，否则并发时会被别的调用覆盖。
# 结构：model / prompt_tokens / completion_tokens / total_tokens / cached_tokens
#       calls（本轮实际发出的请求数，含重试）/ estimated（端点未回 usage，按字符估算）
LAST_USAGE: dict = {}
_EMPTY = {"model": "", "prompt_tokens": 0, "completion_tokens": 0,
          "total_tokens": 0, "cached_tokens": 0, "calls": 0, "estimated": False}


def reset_usage() -> None:
    LAST_USAGE.clear()
    LAST_USAGE.update(dict(_EMPTY))


def _estimate(text: str) -> int:
    """端点不回 usage 时的粗估：中文按 1.5 字 1 token，好过不记。"""
    return max(0, int(len(text or "") / 1.5))


def _note_usage(targets: list[dict], model_id: str, usage: dict | None,
                messages: list[dict], content: str) -> None:
    p = c = cached = 0
    if usage:
        p = int(usage.get("prompt_tokens") or 0)
        c = int(usage.get("completion_tokens") or 0)
        cached = int(usage.get("prompt_cache_hit_tokens")
                     or (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    estimated = False
    if not p and not c:
        p = _estimate("".join(str(m.get("content") or "") for m in messages))
        c = _estimate(content)
        estimated = True
    for t in targets:
        if not t:
            continue
        for k, v in _EMPTY.items():
            t.setdefault(k, v)
        t["model"] = model_id
        t["prompt_tokens"] += p
        t["completion_tokens"] += c
        t["total_tokens"] += p + c
        t["cached_tokens"] += cached
        t["calls"] += 1
        t["estimated"] = t["estimated"] or estimated


def list_models() -> list[dict]:
    if not MODELS_JSON.exists():
        return []
    data = json.loads(MODELS_JSON.read_text(encoding="utf-8"))
    out = []
    for m in data:
        if not m.get("url") or not m.get("apiKey"):
            continue
        out.append({
            "id": m["id"],
            "name": m.get("name") or m["id"],
            "vendor": m.get("vendor") or "",
            "context": m.get("maxInputTokens"),
        })
    return out


# ── 模型体检 ────────────────────────────────────────────────────────────
# models.json 里有些条目在 WorkBuddy 内能用（它会补请求头、拼路径），
# 从本进程直接调却不成立：OpenCode 那组要 x-opencode-session，
# GLM-5.3-Flash 的 url 少了 /chat/completions，sensenova 的 id 对不上。
# 下拉里列出来就是让人选一个必报错的，所以先探活，只列通得过的。
HEALTH_JSON = Path(__file__).resolve().parent.parent / "data" / "model_health.json"
PROBE_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")


def probe_model(m: dict, timeout: int = 20) -> tuple[bool, str]:
    """发一个 max_tokens=1 的请求，只看端点认不认这条路径与模型名。

    带浏览器 UA：有些网关按 UA 拦请求，用 Python 默认 UA 会拿到 403，
    那是假阴性——失败原因也会记成 Cloudflare 而不是真实原因。
    """
    body = {"model": m["id"], "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1, "stream": False}
    req = urllib.request.Request(
        m["url"], data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {m['apiKey']}",
                 "User-Agent": PROBE_UA, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read(64)
        return True, ""
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "ignore")
        msg = raw
        try:
            j = json.loads(raw)
            err = j.get("error")
            msg = (err.get("message") if isinstance(err, dict) else err) or j.get("message") or raw
        except (json.JSONDecodeError, AttributeError):
            pass
        return False, f"HTTP {e.code}: {str(msg)[:90]}"
    except Exception as e:  # noqa: BLE001 —— 网络层的什么都算不通
        return False, f"{type(e).__name__}: {e}"[:100]


def load_health() -> dict:
    if not HEALTH_JSON.exists():
        return {}
    try:
        return json.loads(HEALTH_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def check_all(force: bool = False) -> dict:
    """逐个探活并落盘。结果按 id 存，附时间戳与失败原因。"""
    cache = load_health()
    got: dict = {}
    for m in list_models():
        full = _get_model(m["id"])
        ok, why = probe_model(full)
        got[m["id"]] = {"ok": ok, "why": why}
    cache = {"ts": datetime.now().isoformat(timespec="seconds"), "results": got}
    try:
        HEALTH_JSON.parent.mkdir(parents=True, exist_ok=True)
        tmp = HEALTH_JSON.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, HEALTH_JSON)
    except OSError:
        pass
    return cache


def usable_models() -> tuple[list[dict], int]:
    """返回（可用的模型, 未通过数量）。没体检过就只给默认那一个。"""
    models = list_models()
    cache = load_health().get("results") or {}
    if not cache:
        d = pick_default(models)
        return ([m for m in models if m["id"] == d], max(0, len(models) - 1))
    ok = [m for m in models if (cache.get(m["id"]) or {}).get("ok")]
    if not ok:  # 全挂时别让下拉空着，至少留默认那条让人能看到报错
        d = pick_default(models)
        ok = [m for m in models if m["id"] == d]
    return ok, max(0, len(models) - len(ok))


def pick_default(models: list[dict]) -> str | None:
    for p in PREFERRED:
        for m in models:
            if p in m["id"].lower():
                return m["id"]
    return models[0]["id"] if models else None


def _get_model(model_id: str) -> dict:
    data = json.loads(MODELS_JSON.read_text(encoding="utf-8"))
    for m in data:
        if m.get("id") == model_id:
            return m
    raise RuntimeError(f"未找到模型 {model_id}")


def chat(messages: list[dict], model_id: str | None = None, temperature: float = 0.3,
         max_tokens: int = 8000, timeout: int = 600, thinking: bool = False,
         usage_out: dict | None = None) -> str:
    """调用模型。

    thinking=False 时对支持推理的模型显式关闭思考：
    长文 JSON 生成场景下，推理 token 会挤占输出预算，导致 content 为空、
    整份报告只剩 reasoning_content。关闭后同一提示词可稳定输出完整 JSON。

    usage_out：传入一个空 dict，函数会把本次（含重试）的 token 用量写进去。
    每个调用方用自己的 dict，互不干扰。
    """
    models = list_models()
    model_id = model_id or pick_default(models)
    if not model_id:
        raise RuntimeError("没有可用模型，请先在 WorkBuddy 中配置模型")
    m = _get_model(model_id)
    reset_usage()
    local: dict = dict(_EMPTY)
    body = {
        "model": m["id"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": min(max_tokens, int(m.get("maxOutputTokens") or max_tokens)),
        "stream": False,
    }
    if not thinking and m.get("reasoning"):
        body["thinking"] = {"type": "disabled"}
    req = urllib.request.Request(
        m["url"], data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {m['apiKey']}"},
    )
    last = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = json.loads(r.read().decode("utf-8", "ignore"))
            choice = payload["choices"][0]
            content = (choice["message"].get("content") or "").strip()
            _note_usage([local, LAST_USAGE], m["id"], payload.get("usage"), messages, content)
            if content:
                if usage_out is not None:
                    usage_out.update(local)
                return content
            # content 为空：通常是 token 预算被推理耗尽
            usage = payload.get("usage") or {}
            last = RuntimeError(
                f"模型返回空正文（finish_reason={choice.get('finish_reason')}，"
                f"tokens={usage.get('completion_tokens')}，"
                f"其中推理 {usage.get('completion_tokens_details', {}).get('reasoning_tokens')}）"
            )
            if attempt == 0:
                body["thinking"] = {"type": "disabled"}
                body["max_tokens"] = min(max_tokens * 2, 32000)
                req = urllib.request.Request(
                    m["url"], data=json.dumps(body, ensure_ascii=False).encode(),
                    headers={"Content-Type": "application/json",
                             "Authorization": f"Bearer {m['apiKey']}"},
                )
        except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError) as e:
            last = e
    if usage_out is not None:  # 失败也把已花掉的 token 交出去
        usage_out.update(local)
    raise RuntimeError(f"模型调用失败：{last}")


def parse_json(text: str) -> Any:
    """从模型输出里抠出 JSON（容忍 ```json 围栏与前后闲话）。"""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    start = min([i for i in (t.find("{"), t.find("[")) if i >= 0], default=-1)
    if start < 0:
        raise ValueError("模型输出中未找到 JSON")
    # 括号配平扫描，兼容字符串内的花括号与转义
    open_ch = t[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth, in_str, esc = 0, False, False
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(t[start:i + 1])
                except json.JSONDecodeError:
                    break
    raise ValueError("模型输出的 JSON 无法解析")
