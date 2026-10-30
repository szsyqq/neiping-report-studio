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
SEARCH_JSON = DATA / "search.json"
BUDGET_JSON = DATA / "budget.json"

# 单次生成的花费上限（元）。0 表示不限制。
DEFAULT_MAX_COST = 2.0


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


# ── 外网搜索 ────────────────────────────────────────────────────────────
# 补材料里没有的东西（行业数据、近期事件）时用。不是必需：没配就当这个功能不存在，
# 生成照常跑，只是不会去外网找。
SEARCH_PROVIDERS = {
    "deepseek": {"name": "DeepSeek 搜索", "url": "https://api.deepseek.com/v1/tools/search",
                 "note": "与模型共用 Key，不必另申请；每千次成功请求 5 元，无结果不计费",
                 "own_key": False},
    "bocha": {"name": "博查", "url": "https://api.bochaai.com/v1/web-search",
              "note": "国内直连，可选公众号（summary=true 带摘要）", "own_key": True},
    "serper": {"name": "Serper（Google）", "url": "https://google.serper.dev/search",
               "note": "结果更全，需要能连 Google 的网络", "own_key": True},
    "tavily": {"name": "Tavily", "url": "https://api.tavily.com/search",
               "note": "为问答场景优化，返回内容较长", "own_key": True},
}


def search_file() -> dict:
    return _read(SEARCH_JSON)


def budget() -> dict:
    """单次生成的成本上限。默认 2 元——参照「一次出稿不该比一杯咖啡贵」。"""
    f = _read(BUDGET_JSON)
    try:
        cap = float(_env("MAX_COST_PER_REPORT") or f.get("maxCost") or DEFAULT_MAX_COST)
    except (TypeError, ValueError):
        cap = DEFAULT_MAX_COST
    cap = max(0.0, min(100.0, cap))
    return {"maxCost": round(cap, 2), "enabled": cap > 0,
            "source": "env" if _env("MAX_COST_PER_REPORT") else ("file" if f else "default")}


def save_budget(patch: dict) -> dict:
    cur = _read(BUDGET_JSON)
    if patch.get("maxCost") is not None:
        try:
            v = float(patch["maxCost"])
            cur["maxCost"] = round(max(0.0, min(100.0, v)), 2)
        except (TypeError, ValueError):
            pass
    if patch.get("clear"):
        cur = {}
    _write(BUDGET_JSON, cur)
    return budget()


def _llm_key_for_search() -> tuple[str, str]:
    """DeepSeek 搜索与模型共用 Key：直接读模型配置里那把 DeepSeek 的。

    list_models() 故意不回显 apiKey（页面要用），所以这里走 _get_model 取完整配置。
    优先取用户当前默认的那个模型，它才是真正在花钱的那个。
    """
    import llm
    try:
        models = llm.list_models()
    except Exception:                                          # noqa: BLE001
        return "", ""
    ids = [m.get("id") for m in models if "deepseek" in str(m.get("id") or "").lower()]
    if not ids:
        return "", ""
    try:
        pref = llm.pick_default(models)
    except Exception:                                          # noqa: BLE001
        pref = ""
    order = ([pref] if pref in ids else []) + [i for i in ids if i != pref]
    for mid in order:
        try:
            m = llm._get_model(mid)                            # noqa: SLF001
        except Exception:                                      # noqa: BLE001
            continue
        key = (m or {}).get("apiKey") or ""
        if key:
            # 搜索接口固定在 /v1/tools/search，但 base 要跟着这把 key 走
            base = str(m.get("url") or "")
            for suf in ("/chat/completions", "/v1/chat/completions"):
                if base.endswith(suf):
                    base = base[: -len(suf)]
                    break
            return key, base.rstrip("/") + "/v1/tools/search"
    return "", ""


def search_config() -> dict:
    """外网搜索配置。环境变量 > data/search.json > 复用模型 Key。

    默认 provider 是 deepseek——它跟模型用同一个 Key，不必为「能不能联网查一句」
    再去申请一个账号。选了它但模型配的是别家（没有 DeepSeek Key）时不算就绪，
    页面会明说差什么。
    """
    f = search_file()
    prov = (_env("SEARCH_PROVIDER") or str(f.get("provider") or "") or "deepseek").strip()
    meta = SEARCH_PROVIDERS.get(prov) or {}
    own = bool(meta.get("own_key", True))

    if own:
        key = _env("SEARCH_API_KEY") or str(f.get("apiKey") or "")
        src = "env" if _env("SEARCH_API_KEY") else ("file" if f.get("apiKey") else "")
        hint = "" if key else "这家要单独的 Key，填在下面或用环境变量 SEARCH_API_KEY"
    else:
        key, _ = _llm_key_for_search()
        src = "llm" if key else ""
        hint = "" if key else "模型里没有配置 DeepSeek Key，先在「模型」里加一个"

    url = (_env("SEARCH_API_URL") or str(f.get("url") or "") or meta.get("url", ""))
    return {"provider": prov, "url": url, "apiKey": key, "source": src,
            "own_key": own, "hint": hint,
            "ready": bool(key and url), "max_results": int(f.get("maxResults") or 5)}


def save_search(patch: dict) -> dict:
    cur = search_file()
    for k in ("provider", "url"):
        if patch.get(k) is not None:
            cur[k] = str(patch[k]).strip()
    if patch.get("maxResults") is not None:
        try:
            cur["maxResults"] = max(1, min(10, int(patch["maxResults"])))
        except (TypeError, ValueError):
            pass
    if (patch.get("apiKey") or "").strip():
        cur["apiKey"] = str(patch["apiKey"]).strip()
    if patch.get("clear"):
        cur = {}
    _write(SEARCH_JSON, cur)
    return search_config()


# ── 汇总（给页面用；密钥只报有没有，不回显） ──────────────────────────
def overview() -> dict:
    from auth import is_default
    import llm
    import mailer

    lc = llm_config()
    mc = mailer.describe()
    cc = caihui_config()
    sc = search_config()
    import pricing
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
        "budget": budget(),
        # 单价给页面直接显示：不想算「2 元够不够」，看到「一次约 ¥0.52」就够了
        "pricing": {"rates": [pricing.rate(i) for i in ("deepseek-flash", "deepseek-v4-pro")],
                    "peak": pricing.is_peak(), "based_on": "DeepSeek 官方价目表 2026-10-30"},
        "search": {"provider": sc["provider"], "url": sc["url"], "ready": sc["ready"],
                   "has_key": bool(sc["apiKey"]), "source": sc["source"],
                   "own_key": sc.get("own_key", True),
                   "max_results": sc["max_results"],
                   "providers": [{"id": k, "name": v["name"], "url": v["url"],
                                  "note": v["note"], "own_key": v.get("own_key", True)}
                                 for k, v in SEARCH_PROVIDERS.items()],
                   "hint": "" if sc["ready"] else (sc.get("hint") or (
                       "没配外网搜索。生成时不会去外网找补充材料。"))},
    }
