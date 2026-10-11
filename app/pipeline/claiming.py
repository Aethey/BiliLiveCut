"""原子任务领取 — 条件 UPDATE + 行数校验 + lease_token。"""

from __future__ import annotations

import uuid

from loguru import logger
from sqlalchemy import DateTime, bindparam
from sqlalchemy import text as sa_text
from sqlmodel import select

from app.db.entities import SegmentTask
from app.db.session import get_session
from app.pipeline.lifecycle import _WORKER_ID, now_utc
from app.pipeline.stage_result import active_stage, make_stage_key


def release_unstarted_claim(claim: SegmentTask | None, queued_stage: str) -> None:
    """撤销关闭期间刚领取但尚未启动的任务，不消耗尝试次数或覆盖新租约。"""
    if claim is None:
        return
    with get_session() as db:
        if db.get_bind().dialect.name == "sqlite":
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        task = db.get(SegmentTask, claim.id)
        if task is None or task.lease_token != claim.lease_token or task.stage != active_stage(queued_stage):
            return
        task.stage = queued_stage
        task.stage_key = make_stage_key(task.segment_id, queued_stage)
        task.attempts = max(0, task.attempts - 1)
        task.claimed_by = task.claimed_at = task.heartbeat_at = task.lease_token = None
        task.started_at = None
        db.add(task)
    logger.info("领取期间关闭，任务重新排队 task_id={} stage={}", claim.id, queued_stage)


def pop_and_claim(queued_stage: str) -> SegmentTask | None:
    """原子领取: 条件 UPDATE + 行数校验 + lease_token。

    对 queued_stage 队列中的第一个可领取任务执行原子 UPDATE,
    同时写入 claimed_by, lease_token, heartbeat_at, started_at,
    attempts++。利用 SQLAlchemy result.rowcount 确保单 Worker 获取。

    :param queued_stage: 排队阶段 (如 QUEUED_FOR_TRANS)。
    :returns: 已被当前 Worker 认领的任务; 无可用任务或竞争失败时返回 None。
    """
    now = now_utc()
    act_stage = active_stage(queued_stage)
    lease_token = uuid.uuid4().hex
    with get_session() as db:
        candidate = db.exec(
            select(SegmentTask.id, SegmentTask.segment_id)
            .where(
                SegmentTask.stage == queued_stage,
                (SegmentTask.next_retry_at.is_(None)) | (SegmentTask.next_retry_at <= now),
            )
            .order_by(SegmentTask.priority.asc(), SegmentTask.created_at.asc())
            .limit(1)
        ).first()

        if candidate is None:
            return None

        task_id, segment_id = candidate

        result = db.exec(
            sa_text(
                """UPDATE segment_tasks
               SET stage = :active,
                   claimed_by = :worker_id,
                   claimed_at = :now,
                   heartbeat_at = :now,
                   lease_token = :lease_token,
                   attempts = attempts + 1,
                   started_at = :now,
                   last_error = NULL
               WHERE id = :task_id
                 AND stage = :queued_stage"""
            ).bindparams(bindparam("now", type_=DateTime())),
            params={
                "active": act_stage,
                "worker_id": _WORKER_ID,
                "now": now,
                "lease_token": lease_token,
                "task_id": task_id,
                "queued_stage": queued_stage,
            },
        )

        if result.rowcount != 1:
            logger.info("原子领取失败 task_id={} 已被其他 Worker 抢占", task_id)
            return None

        task = db.get(SegmentTask, task_id)
        if task is None:
            return None

        logger.info(
            "原子领取成功 task_id={} stage={} segment={} worker={} lease={}",
            task_id,
            act_stage,
            segment_id,
            _WORKER_ID,
            lease_token[:12],
        )
        return task
