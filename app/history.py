"""生成历史：每次生成落一份 draft 全文，便于回看、接着改、重新导出。

为什么单独存而不是只靠 output/ 里的 docx：docx 是排好版的成品，改不回去；
用户真正想保留的是「那一次生成的内容 + 用的哪几份材料 + 花了多少 token」。
所以记录里存 draft 本身，docx 只在导出后回填文件名。

存储分两层：
- data/history/<id>.json    单次记录（含 draft 全文，20–40KB）
- data/history/index.json   摘要列表（面板只读这个，避免一次解析几十份全文）
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DIR = BASE / "data" / "history"
INDEX = DIR / "index.json"
LOCK = threading.Lock()
MAX_ITEMS = 200


def _read_index() -> list[dict]:
    if not INDEX.exists():
        return []
    try:
        data = json.loads(INDEX.read_text(encoding="utf-8"))
        items = data.get("items") if isinstance(data, dict) else data
        return items if isinstance(items, list) else []
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return []


def _write_index(items: list[dict]):
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = INDEX.with_suffix(".tmp")
    tmp.write_text(json.dumps({"items": items}, ensure_ascii=False), encoding="utf-8")
    tmp.replace(INDEX)


def _summary(rec: dict) -> dict:
    d = rec.get("draft") or {}
    return {
        "id": rec.get("id", ""),
        "ts": rec.get("ts", ""),
        "company": rec.get("company", ""),
        "model": rec.get("model", ""),
        "tokens": rec.get("tokens", 0),
        "seconds": rec.get("seconds", 0),
        "chars": ((d.get("validation") or {}).get("body_chars") or 0),
        "materials": [m.get("title", "") for m in (rec.get("materials") or [])],
        "docx": rec.get("docx", ""),
        "mail": rec.get("mail") or {},
        "issues": len(((d.get("validation") or {}).get("issues")) or []),
    }


def save(draft: dict, materials: list[dict], model: str = "", seconds: float = 0.0) -> str:
    """落一条记录，返回 id。draft 会被原样保存（含 usage/validation）。"""
    now = datetime.now()
    base = (draft.get("company_full_name") or draft.get("company") or "未命名").strip()
    rid = now.strftime("%Y%m%d-%H%M%S") + "-" + hashlib.sha1(
        (base + now.isoformat()).encode()).hexdigest()[:6]
    usage = draft.get("usage") or {}
    rec = {
        "id": rid,
        "ts": now.isoformat(timespec="seconds"),
        "company": base,
        "model": model or usage.get("model", ""),
        "tokens": usage.get("total_tokens", 0),
        "seconds": round(seconds or usage.get("seconds") or 0, 1),
        "materials": [{"title": m.get("title", ""), "pages": m.get("pages", 0),
                       "chars": m.get("chars", 0), "path": m.get("path", "")}
                      for m in materials],
        "draft": draft,
        "docx": "",
    }
    DIR.mkdir(parents=True, exist_ok=True)
    path = DIR / f"{rid}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)

    with LOCK:
        items = _read_index()
        items.insert(0, _summary(rec))
        dropped = items[MAX_ITEMS:]
        items = items[:MAX_ITEMS]
        _write_index(items)
        for old in dropped:  # 超出上限的正文一并删掉，别只删索引
            try:
                (DIR / f"{old.get('id')}.json").unlink()
            except OSError:
                pass
    return rid


def list_records(limit: int = 60) -> list[dict]:
    return _read_index()[:limit]


def get(rid: str) -> dict | None:
    safe = "".join(c for c in (rid or "") if c.isalnum() or c == "-")
    if not safe:
        return None
    path = DIR / f"{safe}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None


def attach_docx(rid: str, docx_name: str):
    """导出后把文件名回填到记录，历史列表里就能直接下载成品。"""
    with LOCK:
        items = _read_index()
        hit = next((x for x in items if x.get("id") == rid), None)
        if hit:
            hit["docx"] = docx_name
            _write_index(items)
    rec = get(rid)
    if rec:
        rec["docx"] = docx_name
        path = DIR / f"{rid}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)


def attach_mail(rid: str, to: list[str], subject: str):
    """发过邮件后记一笔，历史列表里能看到「已发送给谁」。"""
    stamp = datetime.now().isoformat(timespec="seconds")
    with LOCK:
        items = _read_index()
        hit = next((x for x in items if x.get("id") == rid), None)
        if hit:
            hit["mail"] = {"to": to, "subject": subject, "ts": stamp}
            _write_index(items)
    rec = get(rid)
    if rec:
        rec["mail"] = {"to": to, "subject": subject, "ts": stamp}
        path = DIR / f"{rid}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)


def update_validation(rid: str, validation: dict):
    """旧记录重算过校验后，把新的 validation 落盘，并同步索引里的待核对条数。

    只换 validation 一个字段，draft 里的正文、用户改过的内容一概不动。
    """
    rec = get(rid)
    if not rec:
        return
    draft = rec.get("draft") or {}
    draft["validation"] = validation
    rec["draft"] = draft
    path = DIR / f"{rid}.json"
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        return
    with LOCK:
        items = _read_index()
        hit = next((x for x in items if x.get("id") == rid), None)
        if hit:
            hit["issues"] = len(validation.get("issues") or [])
            _write_index(items)


def remove(rid: str) -> bool:
    safe = "".join(c for c in (rid or "") if c.isalnum() or c == "-")
    if not safe:
        return False
    with LOCK:
        items = [x for x in _read_index() if x.get("id") != safe]
        _write_index(items)
    try:
        (DIR / f"{safe}.json").unlink()
        return True
    except OSError:
        return False
