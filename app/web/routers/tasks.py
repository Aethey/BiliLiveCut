"""任务队列."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

_MAX_QUERY_LIMIT = 500
_MAX_QUERY_DAYS = 365


def _clamp(v, lo, hi):
    return max(lo, min(v, hi))


router = APIRouter()


@router.get("/tasks")
def get_tasks(limit: int = 50, stage: str | None = None) -> dict[str, Any]:
    """返回任务队列列表及各阶段统计。"""
    limit = _clamp(limit, 1, _MAX_QUERY_LIMIT)
    from app.db.session import get_session
    from app.pipeline.task_worker import get_worker
    from app.pipeline.task_worker import list_tasks as _list
    from app.web.services.source_identity import source_identities_for_sessions, unknown_source_identity

    tasks = _list(limit=limit, stage=stage)
    with get_session() as db:
        sources = source_identities_for_sessions(db, (int(task["session_id"]) for task in tasks))
    for task in tasks:
        task.update(sources.get(int(task["session_id"]), unknown_source_identity()))
    return {"tasks": tasks, "stats": get_worker().stats}


@router.post("/tasks/{task_id}/retry")
def retry_task(task_id: int) -> dict[str, Any]:
    """手动重试一个失败/取消的任务。"""
    from app.pipeline.task_worker import retry_task as _retry

    ok = _retry(task_id)
    if not ok:
        raise HTTPException(status_code=400, detail="任务不存在或状态不允许重试")
    return {"status": "retried"}


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: int) -> dict[str, str]:
    """请求取消；返回取消中或已经取消的真实阶段。"""
    from app.db.entities import SegmentTask
    from app.db.session import get_session
    from app.pipeline.task_worker import cancel_task as _cancel

    ok = _cancel(task_id)
    if not ok:
        raise HTTPException(status_code=400, detail="任务不存在、已终结或正在发布；发布结果必须完成或核对")
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="任务已不存在，请刷新任务列表")
        return {"status": task.stage}
