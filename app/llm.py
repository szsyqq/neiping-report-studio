"""大模型调用层。

直接复用 WorkBuddy 已配置的模型（~/.workbuddy/models.json），
不需要另外填 API Key；调用走 OpenAI 兼容的 /chat/completions。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

MODELS_JSON = Path.home() / ".workbuddy" / "models.json"

# 适合长文写作的偏好顺序（上下文大、中文好、性价比高）
PREFERRED = ["deepseek-v4", "kimi-k2", "glm-5.1", "qwen3.7-plus", "qwen3.7-max", "glm-5"]


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
         max_tokens: int = 8000, timeout: int = 600, thinking: bool = False) -> str:
    """调用模型。

    thinking=False 时对支持推理的模型显式关闭思考：
    长文 JSON 生成场景下，推理 token 会挤占输出预算，导致 content 为空、
    整份报告只剩 reasoning_content。关闭后同一提示词可稳定输出完整 JSON。
    """
    models = list_models()
    model_id = model_id or pick_default(models)
    if not model_id:
        raise RuntimeError("没有可用模型，请先在 WorkBuddy 中配置模型")
    m = _get_model(model_id)
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
            if content:
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
