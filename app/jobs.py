"""长任务的进度台账。

取数、下载材料、调用大模型都是几十秒到几分钟的活，同步 HTTP 请求让前端
无法显示进度，也容易被浏览器/代理掐断。这里把它们放进后台线程，前端按
job_id 轮询当前百分比、阶段文案与逐份材料的明细。
"""

from __future__ import annotations

import threading
import time
import traceback
import uuid

_LOCK = threading.Lock()
_JOBS: dict[str, dict] = {}
_KEEP_SECONDS = 1800


def _gc():
    now = time.time()
    for jid in [k for k, v in _JOBS.items() if now - v["_t_end"] > _KEEP_SECONDS]:
        _JOBS.pop(jid, None)


def snapshot(job: dict) -> dict:
    """返回给前端的视图，去掉内部字段。"""
    with _LOCK:
        out = {k: v for k, v in job.items() if not k.startswith("_")}
        out["elapsed"] = round(time.time() - job["_t0"], 1)
    return out


def get(job_id: str) -> dict | None:
    with _LOCK:
        job = _JOBS.get(job_id)
    return snapshot(job) if job else None


def submit(kind: str, work) -> dict:
    """work(progress) -> 结果 dict。progress(pct, stage, detail, items)。"""
    job_id = f"{kind}-{uuid.uuid4().hex[:10]}"
    job = {
        "id": job_id, "kind": kind, "state": "running",
        "pct": 0, "stage": "", "detail": "", "items": [],
        "result": None, "error": "", "_t0": time.time(), "_t_end": time.time(),
    }
    with _LOCK:
        _JOBS[job_id] = job
        _gc()

    def progress(pct=None, stage=None, detail=None, items=None):
        with _LOCK:
            if pct is not None:
                # 进度只前进不后退，避免多线程回调抖动
                job["pct"] = max(job["pct"], int(max(0, min(99, pct))))
            if stage is not None:
                job["stage"] = stage
            if detail is not None:
                job["detail"] = detail
            if items is not None:
                job["items"] = items

    def run():
        try:
            result = work(progress)
            with _LOCK:
                job.update(state="done", pct=100, result=result,
                           detail="完成", _t_end=time.time())
        except Exception as e:  # noqa: BLE001
            with _LOCK:
                job.update(state="error", error=str(e),
                           detail=str(e), _t_end=time.time(),
                           _trace=traceback.format_exc()[-1500:])

    threading.Thread(target=run, daemon=True).start()
    return snapshot(job)
