"""用量统计：每次模型调用与每个动作记一行，落在 data/usage.json。

为什么落盘而不是内存计数：服务是后台进程，被系统回收后内存清零，
统计一旦随重启归零就没有意义。落盘后重启仍能看到本月、累计。

记录口径：
- generate：一次模型写作，带 prompt/completion/total token（模型没回 usage 时按字符估算，标 estimated）。
- search / fetch / export：只记次数与规模，不产生 token；用来回答「这套东西用了多少次」。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
FILE = DATA / "usage.json"
LOCK = threading.Lock()
MAX_EVENTS = 3000

KIND_LABEL = {
    "generate": "生成草稿",
    "search": "检索公告",
    "fetch": "读取材料",
    "export": "导出报告",
}


def _load() -> dict:
    if not FILE.exists():
        return {"events": []}
    try:
        data = json.loads(FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("events"), list):
            return data
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        pass
    return {"events": []}


def record(kind: str, *, company: str = "", model: str = "", prompt_tokens: int = 0,
           completion_tokens: int = 0, total_tokens: int = 0, cached_tokens: int = 0,
           seconds: float = 0.0, chars: int = 0, ok: bool = True,
           estimated: bool = False, note: str = "", mcp_calls: int = 0) -> dict:
    """记一行。token 三项只在 generate 时有值；mcp_calls 是本次消耗的财汇点数。"""
    total = int(total_tokens or (prompt_tokens + completion_tokens))
    ev = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "kind": kind,
        "company": (company or "")[:60],
        "model": model or "",
        "prompt_tokens": int(prompt_tokens or 0),
        "completion_tokens": int(completion_tokens or 0),
        "total_tokens": total,
        "cached_tokens": int(cached_tokens or 0),
        "seconds": round(float(seconds or 0), 1),
        "chars": int(chars or 0),
        "ok": bool(ok),
        "estimated": bool(estimated),
        "note": (note or "")[:160],
        "mcp_calls": int(mcp_calls or 0),
    }
    with LOCK:
        data = _load()
        events = data.get("events") or []
        events.append(ev)
        data["events"] = events[-MAX_EVENTS:]
        FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(FILE)  # 原子替换：进程中途被杀也不会留下半个 JSON
    return ev


def _bucket(events: list[dict]) -> dict:
    gen = [e for e in events if e["kind"] == "generate"]
    return {
        "calls": len(gen),
        "prompt_tokens": sum(e["prompt_tokens"] for e in gen),
        "completion_tokens": sum(e["completion_tokens"] for e in gen),
        "total_tokens": sum(e["total_tokens"] for e in gen),
        "cached_tokens": sum(e["cached_tokens"] for e in gen),
        "seconds": round(sum(e["seconds"] for e in gen), 1),
        "chars": sum(e["chars"] for e in gen),
        "estimated": any(e["estimated"] for e in gen),
    }


def summary() -> dict:
    events = _load().get("events") or []
    now = datetime.now()
    today_key = now.strftime("%Y-%m-%d")

    def within(prefix: str) -> list[dict]:
        return [e for e in events if str(e.get("ts", "")).startswith(prefix)]

    def mcp_sum(rows: list[dict]) -> int:
        return sum(int(e.get("mcp_calls") or 0) for e in rows)

    def act_sum(rows: list[dict]) -> int:
        """动作次数：检索、读取、生成、导出各有一次就算一次，不含模型重试。"""
        return len([e for e in rows if e.get("kind") != "generate"])

    months = sorted({str(e.get("ts", ""))[:7] for e in events if e.get("ts")})
    kinds = {k: 0 for k in KIND_LABEL}
    for e in events:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1

    by_model: dict[str, dict] = {}
    for e in events:
        if e["kind"] != "generate":
            continue
        row = by_model.setdefault(e["model"] or "未知模型",
                                  {"model": e["model"] or "未知模型", "calls": 0,
                                   "total_tokens": 0, "seconds": 0.0})
        row["calls"] += 1
        row["total_tokens"] += e["total_tokens"]
        row["seconds"] = round(row["seconds"] + e["seconds"], 1)

    today = within(today_key)

    # 时间线：所有动作都列，用户要看的是「这一次点了几下、花了多少 token」
    timeline = [
        {"ts": e["ts"], "kind": e["kind"], "company": e.get("company", ""),
         "model": e.get("model", ""), "mcp_calls": int(e.get("mcp_calls") or 0),
         "total_tokens": e["total_tokens"], "seconds": e["seconds"],
         "ok": e.get("ok", True), "note": e.get("note", "")}
        for e in reversed(events)
    ][:40]

    return {
        "today": _bucket(today),
        "total": _bucket(events),
        "mcp": {"today": mcp_sum(today), "total": mcp_sum(events)},
        "actions": {"today": act_sum(today), "total": act_sum(events)},
        "kinds": kinds,
        "by_model": sorted(by_model.values(), key=lambda x: -x["total_tokens"]),
        "timeline": timeline,
        "recent": [t for t in timeline if t["kind"] == "generate"][:8],
        "months": months[-6:],
        "since": events[0]["ts"] if events else "",
    }
