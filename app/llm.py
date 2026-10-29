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

# 云端部署没有 ~/.workbuddy/models.json，用环境变量配一个模型就够：
#   LLM_BASE_URL=https://api.deepseek.com/chat/completions
#   LLM_API_KEY=sk-...
#   LLM_MODEL=deepseek-v4-flash
# 三个都给齐时优先用环境变量，忽略本地文件。
_ENV_MODEL = ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")


def _file_model() -> dict | None:
    """页面「设置」里填的自定义模型（data/llm.json）。本机与云端都能用这条。"""
    f = Path(__file__).resolve().parent.parent / "data" / "llm.json"
    if not f.exists():
        return None
    try:
        d = json.loads(f.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError):
        return None
    if not (d.get("url") and d.get("apiKey") and d.get("model")):
        return None
    return {"id": d["model"], "name": d["model"], "vendor": "custom",
            "url": d["url"], "apiKey": d["apiKey"],
            "maxInputTokens": int(d.get("maxInput") or 131072),
            "maxOutputTokens": int(d.get("maxOutput") or 32000)}


def _env_model() -> dict | None:
    url, key, mid = (os.environ.get(k, "").strip() for k in _ENV_MODEL)
    if not (url and key and mid):
        return None
    return {"id": mid, "name": mid, "vendor": "env", "url": url, "apiKey": key,
            "maxInputTokens": int(os.environ.get("LLM_MAX_INPUT") or 131072),
            # 输出上限别给 8192：一份报告的 JSON（正文 + 摘要两个池 + 两张表 +
            # data_notes + sources）实测就要 7800 上下 token，8192 只剩几百的余量，
            # 材料稍多就顶到上限被截断，表现为「模型输出的 JSON 无法解析」。
            "maxOutputTokens": int(os.environ.get("LLM_MAX_OUTPUT") or 32000)}


# 适合长文写作的偏好顺序（上下文大、中文好、性价比高）。
# 「deepseek」单独一条排在前面：模型名会随版本变（deepseek-v4-flash、
# deepseek-chat…），只认 deepseek-v4 的话，换了个后缀就默认不到它。
PREFERRED = ["deepseek", "kimi-k2", "glm-5.1", "qwen3.7-plus", "qwen3.7-max", "glm-5"]

# 用户上次在下拉里选的模型。有它就以它为准——否则每次刷新都回落到
# 「偏好顺序里的第一个」，用户选了 A、页面又回到 B，看着像没生效。
PREF_JSON = Path(__file__).resolve().parent.parent / "data" / "model_pref.json"

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
    env = _env_model()
    if env:
        return [{"id": env["id"], "name": env["name"], "vendor": env["vendor"],
                 "context": env["maxInputTokens"]}]
    fm = _file_model()
    if fm:
        # 页面填的模型排在最前，其余照旧列出（能用的）
        rest = []
        if MODELS_JSON.exists():
            for m in json.loads(MODELS_JSON.read_text(encoding="utf-8")):
                if m.get("url") and m.get("apiKey") and m.get("id") != fm["id"]:
                    rest.append({"id": m["id"], "name": m.get("name") or m["id"],
                                 "vendor": m.get("vendor") or "",
                                 "context": m.get("maxInputTokens")})
        return [{"id": fm["id"], "name": fm["name"], "vendor": fm["vendor"],
                 "context": fm["maxInputTokens"]}] + rest
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
    """默认选哪个：设置页填的 > 页面上次选的 > 偏好顺序 > 第一个。

    顺序不能反：设置页是显式配置，页面上次的选择只是习惯。反过来会出现
    「在设置里改成 A，刷新又回到 B」——比不记还糟。
    """
    ids = {m["id"] for m in models}
    fm = _file_model()
    if fm and fm["id"] in ids:
        return fm["id"]
    pref = _pref_model()
    if pref and pref in ids:
        return pref
    for p in PREFERRED:
        for m in models:
            if p in m["id"].lower():
                return m["id"]
    return models[0]["id"] if models else None


def _pref_model() -> str:
    """上次选中的模型（页面写入）。读不到就当没有，不抛。"""
    try:
        d = json.loads(PREF_JSON.read_text(encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError):
        return ""
    return str(d.get("model") or "").strip()


def save_pref(model_id: str) -> None:
    """记住用户在页面上选的模型。写失败不影响使用，静默即可。"""
    if not model_id:
        return
    try:
        PREF_JSON.parent.mkdir(parents=True, exist_ok=True)
        tmp = PREF_JSON.with_suffix(".tmp")
        tmp.write_text(json.dumps({"model": model_id}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, PREF_JSON)
    except OSError:
        pass


def diagnose() -> dict:
    """模型这一层的自检：能不能用、默认是哪个、不能用的原因、缺什么配置。

    页面下拉报「没有可用模型」时，真正需要的是最后那一句 hint——
    「没有可用模型」这句话本身没有信息量，用户不知道去哪儿补。
    """
    models = list_models()
    cache = load_health()
    ids = {m["id"] for m in models}
    reasons = {k: v.get("why") for k, v in (cache.get("results") or {}).items()
               if not v.get("ok")}
    # 只统计「这次清单里」没通过的：换过配置后，缓存里还留着别的模型的名字，
    # 原样报「8 个未通过」会让人以为这 8 个都在下拉里。
    reasons = {k: v for k, v in reasons.items() if k in ids}
    hint = ""
    if not models:
        env_url = (os.environ.get("LLM_BASE_URL") or "").strip()
        env_mid = (os.environ.get("LLM_MODEL") or "").strip()
        if env_url or env_mid:
            hint = ("只配了一半：LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 三个都要有，"
                    "缺一个这条通道就不生效。")
        elif not MODELS_JSON.exists():
            hint = ("没有读到任何模型配置。本机应有 ~/.workbuddy/models.json；"
                    "云端应在 start.sh 里写 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL；"
                    "也可以在下面「模型」里手填一个。")
        else:
            hint = "~/.workbuddy/models.json 里没有同时带 url 与 apiKey 的条目。"
    return {
        "ready": bool(models),
        "models": models,
        "default": pick_default(models),
        "skipped": len(reasons),
        "reasons": reasons,
        "hint": hint,
        "source": ("env" if _env_model() else ("file" if _file_model() else
                   ("workbuddy" if models else ""))),
        "checked_at": cache.get("ts") or "",
    }


def _get_model(model_id: str) -> dict:
    env = _env_model()
    if env and model_id == env["id"]:
        return env
    fm = _file_model()
    if fm and model_id == fm["id"]:
        return fm
    if not MODELS_JSON.exists():
        raise RuntimeError("未找到模型配置：本地需要 ~/.workbuddy/models.json，"
                           "云端需要 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL")
    data = json.loads(MODELS_JSON.read_text(encoding="utf-8"))
    for m in data:
        if m.get("id") == model_id:
            return m
    raise RuntimeError(f"未找到模型 {model_id}")


def context_limit(model_id: str | None = None) -> int:
    """这个模型能吃多少输入 token。取不到就给个保守值，调用方据此裁材料。

    单独做成函数，是因为 generate 要在拼提示词之前就知道上限——拼完再发现
    超了，只能整份重拼，白算一次 8 万字的模板渲染。
    """
    try:
        models = list_models()
        ids = [m["id"] for m in models]
        mid = model_id if model_id in ids else pick_default(models)
        return int(_get_model(mid).get("maxInputTokens") or 131072)
    except Exception:                                          # noqa: BLE001
        return 131072


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
    ids = [m["id"] for m in models]
    if not ids:
        raise RuntimeError("没有可用模型：" + (diagnose().get("hint") or
                                             "请先在 WorkBuddy 中配置模型"))
    # 页面下拉里可能留着占位文案（「没有可用模型」「未连接服务」）或上一版的
    # 模型名，这些都不是 id。直接拿去请求只会得到一句「未找到模型 X」，
    # 而报告其实照常能出——所以认不出来就退回默认，不在这里拦。
    if model_id and model_id not in ids:
        model_id = ""
    model_id = model_id or pick_default(models)
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
            finish = choice.get("finish_reason") or ""
            _note_usage([local, LAST_USAGE], m["id"], payload.get("usage"), messages, content)
            # 截断（finish_reason=length）不在这里抛：半截 JSON 往往只差最后一个
            # 括号，parse_json 能修回来，直接抛等于把可救的稿子扔掉。
            # 但要如实记下来，让上层知道「这次可能不全」，好决定要不要重试。
            local["finish_reason"] = finish
            LAST_USAGE["finish_reason"] = finish
            if content:
                if usage_out is not None:
                    usage_out.update(local)
                return content
            # content 为空：通常是 token 预算被推理耗尽
            usage = payload.get("usage") or {}
            last = RuntimeError(
                f"模型返回空正文（finish_reason={finish}，"
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


def _scan(t: str, start: int) -> tuple[str, bool]:
    """从 start 扫到结尾，返回（未闭合的括号栈, 是否停在字符串中间）。

    栈里只放 '{' 与 '['，用于事后补闭括号。
    """
    stack: list[str] = []
    in_str = esc = False
    for ch in t[start:]:
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
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
    return "".join(stack), in_str


def _repair_truncated(t: str, start: int) -> str | None:
    """把被截断的 JSON 补成能解析的样子。

    输出被 max_tokens 砍断时，模型通常已经写完了正文，只是停在半句或某个
    字段中间。整份作废太浪费——从尾部往回找最近的「结构边界」（逗号、
    已闭合的对象/数组），截断到那里再补上剩余括号，多半就能解析。
    补出来的可能缺尾部字段（sources、data_notes 的后几条），那些由
    _ensure_structure 补默认值，比让用户重跑一次划算。
    """
    stack, in_str = _scan(t, start)
    if not stack and not in_str:
        return None                      # 括号本来就闭合，不是截断问题
    closers = "".join("}" if c == "{" else "]" for c in reversed(stack))
    body = t[start:]
    # 从尾部往前找断点，最多试 240 处，避免在超长文本上空转
    cuts = [i for i, ch in enumerate(body) if ch in ",}]"][-240:]
    for i in reversed(cuts):
        head = body[: i + 1].rstrip()
        if head.endswith(","):
            head = head[:-1]
        if not head:
            continue
        st2, in2 = _scan(head, 0)
        if in2:
            continue                     # 断在字符串里，这个点不能用
        tail = "".join("}" if c == "{" else "]" for c in reversed(st2))
        try:
            json.loads(head + tail)
            return head + tail
        except json.JSONDecodeError:
            continue
    # 退一步：只补括号，赌它正好断在字段边界上
    try:
        json.loads(body + closers)
        return body + closers
    except json.JSONDecodeError:
        return None


def parse_json(text: str) -> Any:
    """从模型输出里抠出 JSON（容忍 ```json 围栏与前后闲话，容忍被截断）。"""
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
    # 走到这里说明 JSON 不完整（多半是被 max_tokens 截断）或中间有非法字符
    fixed = _repair_truncated(t, start)
    if fixed is not None:
        try:
            return json.loads(fixed)
        except json.JSONDecodeError:
            pass
    raise ValueError("模型输出的 JSON 无法解析（通常是被输出上限截断，或引号/逗号未闭合）")
