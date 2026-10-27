"""会话状态落地：把内存里的「这一轮的材料与草稿」存到磁盘。

为什么需要它：`server.STATE` 是纯内存的，后端一重启就全没了。表现是——
材料清单空掉、分析附件（发债情况／财务专项指标）变成「未识别」，
已经生成过的草稿也点不开。而材料是花十几秒逐份解析 PDF 换来的，
丢掉一次就要重来一次。

存的东西与边界：
- 存 facts、materials（含解析后的正文）、base（更新模式的底稿）、draft。
- 不存 LOCK、任务进度、HTTP 会话这类运行期状态——那些重启就该重来。
- 每个主体一份文件，按主体键的哈希命名（主体名里有斜杠、空格也不能出事）。
- 单份超过 `MAX_BYTES` 就整份不存：宁可下次重新解析，也不要把磁盘写爆。
- 只恢复 `MAX_AGE_DAYS` 以内的快照：半年前的草稿自动铺回来，比空着更让人困惑。

这份实现解决的是「进程重启」这一层。容器整体重建（云端）时磁盘也会丢，
那种情况要靠对象存储，不是这里能覆盖的。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DIR = BASE / "data" / "state"
LOCK = threading.Lock()

MAX_BYTES = 12 * 1024 * 1024      # 单份上限
MAX_AGE_DAYS = 14                 # 超过就不恢复
MAX_FILES = 20                    # 只留最近这么多主体的快照

# 只搬这几把键。多搬一个运行期状态，就会造出一份「看起来像活的」的假会话。
KEEP = ("facts", "materials", "base", "draft")


def _slug(key: str) -> str:
    h = hashlib.sha256((key or "").encode("utf-8")).hexdigest()[:16]
    return f"{h}.json"


def _path(key: str) -> Path:
    return DIR / _slug(key)


def save(key: str, st: dict) -> bool:
    """落一份快照。返回是否真的写了。

    没材料也没草稿就不写：那是空会话，存下来只会在下次启动时占个位置。
    """
    if not isinstance(st, dict):
        return False
    body = {k: st.get(k) for k in KEEP if st.get(k)}
    if not body.get("materials") and not body.get("draft"):
        return False
    payload = {"key": key, "saved_at": datetime.now().isoformat(timespec="seconds"), **body}
    try:
        blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        return False              # 状态里有不可序列化的东西，跳过而不是抛
    if len(blob) > MAX_BYTES:
        return False
    try:
        DIR.mkdir(parents=True, exist_ok=True)
        tmp = _path(key).with_suffix(".tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, _path(key))
    except OSError:
        return False
    _prune()
    return True


def _prune() -> None:
    """只留最近 MAX_FILES 份。超出时按修改时间淘汰最旧的。"""
    try:
        files = sorted(DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    for old in files[MAX_FILES:]:
        try:
            old.unlink()
        except OSError:
            pass


def load_all() -> dict[str, dict]:
    """读出所有未过期的快照，返回 {主体键: 状态}。读不了的单份跳过。"""
    out: dict[str, dict] = {}
    if not DIR.is_dir():
        return out
    cutoff = datetime.now() - timedelta(days=MAX_AGE_DAYS)
    for p in DIR.glob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(d, dict):
            continue
        key = str(d.get("key") or "")
        if not key:
            continue
        stamp = str(d.get("saved_at") or "")
        if stamp:
            try:
                if datetime.fromisoformat(stamp) < cutoff:
                    continue
            except ValueError:
                pass
        st = {k: d[k] for k in KEEP if k in d}
        if not st.get("materials") and not st.get("draft"):
            continue
        out[key] = st
    return out


def drop(key: str) -> None:
    try:
        _path(key).unlink()
    except OSError:
        pass


def describe() -> str:
    """启动日志用：恢复了几个主体、几份材料。"""
    n = len(list(DIR.glob("*.json"))) if DIR.is_dir() else 0
    return f"会话快照：磁盘上有 {n} 份" if n else "会话快照：暂无（首次启动属正常）"
