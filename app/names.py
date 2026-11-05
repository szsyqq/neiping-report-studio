"""公司全称的本地词库。

为什么需要它：联想原先只做一件事——拿用户输错的串去公告库查一次。可错得
多一点（把「有限公司」写成「有限责任公司」）就连公告库都查不回来，越是需
要联想的时候越联想不出来。于是把历次检索与联想里见过的全称攒成一个词库，
先在本库里按字面相似度找，找不到再去公告库查，并且查询可以用去掉后缀的短串。

词库落在 data/company_names.json，容器重启会丢；丢了不影响使用，只是第一次
联想要多花一次公告库点数，之后又攒起来。
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
STORE = BASE / "data" / "company_names.json"
HIST = BASE / "data" / "history"

LOCK = threading.RLock()
_CACHE: dict[str, int] | None = None
_BOOTED = False

# 法定后缀，由长到短；截短查询要用它去掉尾巴
_SUFFIX = re.compile(r"(?:股份)?有限责任公司$|有限公司$|集团公司$|集团$|公司$")
# 标题里常有的引导词，抠名字时得先剥掉
_LEAD = re.compile(r"^(?:关于|就|对于|及|暨|以及|和|与|对)+")


def _read() -> dict[str, int]:
    try:
        raw = json.loads(STORE.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return {str(k): int(v or 0) for k, v in raw.items() if k}
    except Exception:                                          # noqa: BLE001
        pass
    return {}


def _write(data: dict[str, int]) -> None:
    try:
        STORE.parent.mkdir(parents=True, exist_ok=True)
        STORE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:                                          # noqa: BLE001
        pass


def _names_in(title: str) -> list[str]:
    """从公告标题里抠公司全称。后缀只认法定全称，否则「…集团有限公司」会被截短。"""
    pat = (r"[\u4e00-\u9fa5A-Za-z0-9（）()·]{2,40}?"
           r"(?:股份有限公司|有限责任公司|有限公司|集团公司)")
    out = []
    for m in pat.finditer(title or ""):
        name = _LEAD.sub("", m.group(0)).lstrip("、， ")
        name = re.sub(r"^(19|20)\d{2}年(度)?|^\d{4}年?", "", name).strip()
        if len(name) >= 6 and not re.search(r"律师|会计师事务所|证券|评级|银行|信托|评估", name):
            out.append(name)
    return out


def bootstrap() -> None:
    """第一次用到时，从历史记录里把见过的公司名搬进来。"""
    global _BOOTED
    if _BOOTED:
        return
    with LOCK:
        if _BOOTED:
            return
        data = _read()
        before = len(data)
        try:
            idx = json.loads((HIST / "index.json").read_text(encoding="utf-8"))
            rows = idx if isinstance(idx, list) else (idx.get("items") or [])
        except Exception:                                      # noqa: BLE001
            rows = []
        for r in rows if isinstance(rows, list) else []:
            co = str((r or {}).get("company") or "").strip()
            if len(co) >= 6:
                data.setdefault(co, 0)
        if len(data) != before:
            _write(data)
        _CACHE = data
        _BOOTED = True


def learn(names) -> None:
    """把见到的全称记下来。只记存在，不累计次数——次数由公告库那次返回给。

    带前缀的残片要挡掉：标题「…延长中国航空资本控股有限责任公司…」会被抠出
    「延长中国航空资本控股有限责任公司」，它比真名多两个字，排出来很像另一家。
    判据是有没有以库里已有的全称结尾——公司全称不会以另一家全称收尾。
    """
    global _CACHE
    want = [n for n in names if n and len(n) >= 6]
    if not want:
        return
    with LOCK:
        data = _CACHE if _CACHE is not None else _read()
        for n in sorted(set(want), key=len):
            if n in data:
                continue
            if any(n != k and n.endswith(k) for k in data):
                continue
            data[n] = 0
        _write(data)
        _CACHE = data


def all_names() -> dict[str, int]:
    """全量词库：名字 → 近 30 条公告里出现的次数（0 表示只在本地见过）。"""
    bootstrap()
    with LOCK:
        return dict(_CACHE or {})


def queries_for(q: str) -> list[str]:
    """拿什么串去公告库查。错得多时原串查不回来，得靠去掉后缀甚至截短的串。"""
    q = (q or "").strip()
    out = [q] if q else []
    bare = _SUFFIX.sub("", q)
    if bare and bare != q and len(bare) >= 4 and bare not in out:
        out.append(bare)
    if len(bare) > 8 and bare[:8] not in out:
        out.append(bare[:8])
    return out[:3]
