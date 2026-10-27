"""设置中心：把散在各处的配置收成一处读写。

页面上的「设置」面板就调这里，四个分区：
    password  访问口令（scrypt，见 auth.py）
    llm       模型 API（base url + key + 模型名）
    mail      发件 SMTP（见 mailer.py）
    caihui    财汇 MCP（url + x-api-key）

每项都是「文件存一份、环境变量可覆盖」。环境变量优先，是因为云端靠它注入密钥：
页面上改了也不该盖掉部署方给的凭据。四个文件都在 data/ 下，gitignore 已覆盖。

为什么不把凭据塞进一个大 json：单文件写坏会连带丢掉别的配置，
分开存、各自原子替换，坏一个不影响其余。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data"

LLM_JSON = DATA / "llm.json"
CAIHUI_JSON = DATA / "caihui.json"


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _env(*names: str) -> str:
    for n in names:
        v = (os.environ.get(n) or "").strip()
        if v:
            return v
    return ""


# ── 模型 ────────────────────────────────────────────────────────────────
def llm_file() -> dict:
    return _read(LLM_JSON)


def llm_config() -> dict:
    """当前生效的模型配置。环境变量 > data/llm.json。"""
    f = llm_file()
    url = _env("LLM_BASE_URL") or str(f.get("url") or "")
    key = _env("LLM_API_KEY") or str(f.get("apiKey") or "")
    mid = _env("LLM_MODEL") or str(f.get("model") or "")
    src = "env" if _env("LLM_BASE_URL") else ("file" if f.get("url") else "")
    return {"url": url, "apiKey": key, "model": mid, "source": src,
            "ready": bool(url and key and mid)}


def save_llm(patch: dict) -> dict:
    cur = llm_file()
    for k, v in (("url", patch.get("url")), ("model", patch.get("model"))):
        if v is not None:
            cur[k] = str(v).strip()
    if (patch.get("apiKey") or "").strip():
        cur["apiKey"] = str(patch["apiKey"]).strip()
    if patch.get("clear"):
        cur = {}
    _write(LLM_JSON, cur)
    return llm_config()


# ── 财汇 ────────────────────────────────────────────────────────────────
def caihui_file() -> dict:
    return _read(CAIHUI_JSON)


def caihui_config() -> dict:
    """财汇端点。三级来源：环境变量 > data/caihui.json > ~/.workbuddy/mcp.json。"""
    f = caihui_file()
    url = _env("CAIHUI_MCP_URL") or str(f.get("url") or "")
    key = _env("CAIHUI_MCP_KEY") or str(f.get("key") or "")
    src = "env" if _env("CAIHUI_MCP_URL") else ("file" if f.get("url") else "")
    if not (url and key):
        # 回落到 WorkBuddy 自己的 mcp.json —— 本机就是这样用的
        try:
            from mcp_client import _load_endpoint
            url, key = _load_endpoint()
            src = "workbuddy"
        except Exception:  # noqa: BLE001
            pass
    return {"url": url, "key": key, "source": src, "ready": bool(url and key)}


def save_caihui(patch: dict) -> dict:
    cur = caihui_file()
    if patch.get("url") is not None:
        cur["url"] = str(patch["url"]).strip()
    if (patch.get("key") or "").strip():
        cur["key"] = str(patch["key"]).strip()
    if patch.get("clear"):
        cur = {}
    _write(CAIHUI_JSON, cur)
    return caihui_config()


# ── 汇总（给页面用；密钥只报有没有，不回显） ──────────────────────────
def overview() -> dict:
    from auth import is_default
    import llm
    import mailer

    lc = llm_config()
    mc = mailer.describe()
    cc = caihui_config()
    try:
        models, skipped = llm.usable_models()
        hint = "" if models else (llm.diagnose().get("hint") or "")
    except Exception:  # noqa: BLE001 —— 配置坏掉时不要让设置页跟着崩
        models, skipped, hint = [], 0, "模型自检失败，看服务日志"
    # 没填自定义端点、但有 WorkBuddy 自带模型时，也算「能用」，
    # 否则徽标写着「未配置」，用户会以为生成功能坏了。
    usable = [m["id"] for m in models]
    llm_ready = lc["ready"] or bool(usable)
    src = lc["source"] or ("workbuddy" if usable else "")
    return {
        "password": {"is_default": is_default()},
        "llm": {"url": lc["url"], "model": lc["model"] or (usable[0] if usable else ""),
                "source": src, "ready": llm_ready, "has_key": bool(lc["apiKey"]),
                "models": usable, "skipped": skipped,
                # 「没有可用模型」这句本身没有信息量，要跟着说缺什么
                "hint": "" if usable else hint},
        "mail": {"host": mc["host"], "port": mc["port"], "user": mc["user"],
                 "tls": mc["tls"], "ready": mc["ready"],
                 "has_password": mc["has_password"], "default_to": mc["default_to"]},
        "caihui": {"url": cc["url"], "source": cc["source"], "ready": cc["ready"],
                   "has_key": bool(cc["key"]),
                   # 没配好时把「该去哪儿填」直接说出来：云端没有 ~/.workbuddy，
                   # 只写「请检查 mcp.json」等于什么也没说。
                   "hint": "" if cc["ready"] else (
                       "没读到财汇端点。云端请在 start.sh 里写 CAIHUI_MCP_URL 与 "
                       "CAIHUI_MCP_KEY 并用 bash start.sh 启动，或在本页「财汇」一栏直接填；"
                       "本机可从 ~/.workbuddy/mcp.json 的 caihui_mcp 条目读取。"),
                   "env_loaded": bool(_env("CAIHUI_MCP_URL"))},
    }
