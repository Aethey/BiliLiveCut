"""任务心跳线程 — 周期更新 heartbeat_at, 检测租约丢失。"""

from __future__ import annotations

import threading

from loguru import logger
from sqlalchemy import DateTime, bindparam
from sqlalchemy import text as sa_text
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session

from app.db.entities import SegmentTask, TaskStatus
from app.db.session import get_session
from app.pipeline.lifecycle import _WORKER_ID, now_utc, shutdown_event

_HEARTBEAT_POLL_S: int = 5


def _log_heartbeat_end(db: Session, task_id: int, expected_stage: str | None) -> None:
    """按实际任务状态解释条件失败，不把正常取消或推进误报为接管。"""
    task = db.get(SegmentTask, task_id)
    stage = task.stage if task is not None else None
    if task is None or stage not in {
        TaskStatus.TRANSCRIBING,
        TaskStatus.ANALYZING,
        TaskStatus.RENDERING,
        TaskStatus.PUBLISHING,
    }:
        logger.info(
            "heartbeat_stopped task={} stage={} expected_stage={} reason=state_changed",
            task_id,
            stage,
            expected_stage,
        )
    else:
        logger.warning(
            "lease_lost task={} stage={} expected_stage={} observed_worker={} reason=ownership_conditions_failed",
            task_id,
            stage,
            expected_stage,
            task.claimed_by,
        )


def start_heartbeat_thread(
    task_id: int,
    lease_token: str | None = None,
    expected_stage: str | None = None,
) -> threading.Event:
    """启动后台心跳线程, 使用租约条件更新 heartbeat_at。

    周期性地以条件 UPDATE 更新 task.heartbeat_at。
    如果 rowcount==0, 当前租约条件已不满足；根据实际状态记录并退出。

    :param task_id: SegmentTask ID。
    :param lease_token: 租约令牌 (UUID hex)。
    :param expected_stage: 期望的阶段状态 (如 TRANSCRIBING)。
    :returns: stop Event, 调用 .set() 停止心跳。
    """
    if not lease_token:
        raise ValueError("心跳更新必须提供租约 token")
    stop = threading.Event()

    def _beat() -> None:
        while not stop.is_set() and not shutdown_event.is_set():
            try:
                with get_session() as db:
                    if lease_token and expected_stage:
                        result = db.exec(
                            sa_text(
                                """UPDATE segment_tasks
                                   SET heartbeat_at = :now
                                   WHERE id = :task_id
                                     AND claimed_by = :worker_id
                                     AND lease_token = :lease_token
                                     AND stage = :expected_stage"""
                            ).bindparams(bindparam("now", type_=DateTime())),
                            params={
                                "now": now_utc(),
                                "task_id": task_id,
                                "worker_id": _WORKER_ID,
                                "lease_token": lease_token,
                                "expected_stage": expected_stage,
                            },
                        )
                        if result.rowcount == 0:
                            _log_heartbeat_end(db, task_id, expected_stage)
                            break
                    elif lease_token:
                        result = db.exec(
                            sa_text(
                                """UPDATE segment_tasks
                                   SET heartbeat_at = :now
                                   WHERE id = :task_id
                                     AND claimed_by = :worker_id
                                     AND lease_token = :lease_token"""
                            ).bindparams(bindparam("now", type_=DateTime())),
                            params={
                                "now": now_utc(),
                                "task_id": task_id,
                                "worker_id": _WORKER_ID,
                                "lease_token": lease_token,
                            },
                        )
                        if result.rowcount == 0:
                            _log_heartbeat_end(db, task_id, expected_stage)
                            break
            except SQLAlchemyError:
                logger.exception("heartbeat_failed: task={} 心跳写入失败，将在下一周期重试", task_id)
            stop.wait(_HEARTBEAT_POLL_S)

    t = threading.Thread(target=_beat, daemon=True, name=f"hb-{task_id}")
    t.start()
    return stop


def clear_heartbeat_if_own(task_id: int, lease_token: str | None = None) -> None:
    """条件清除 heartbeat, 必须携带租约。

    在任务完成后将 heartbeat_at 设为 NULL, 防止被 stale recovery 误判。
    使用租约条件 UPDATE, 租约已转移时跳过。

    :param task_id: SegmentTask ID。
    :param lease_token: 租约令牌。
    """
    if not lease_token:
        logger.debug("heartbeat_clear_refused task={} reason=missing_lease", task_id)
        return
    try:
        with get_session() as db:
            if lease_token:
                result = db.exec(
                    sa_text(
                        """UPDATE segment_tasks
                           SET heartbeat_at = NULL
                           WHERE id = :task_id
                             AND claimed_by = :worker_id
                             AND lease_token = :lease_token"""
                    ),
                    params={
                        "task_id": task_id,
                        "worker_id": _WORKER_ID,
                        "lease_token": lease_token,
                    },
                )
                if result.rowcount == 0:
                    logger.debug("clear_heartbeat_if_own: task={} 租约已转移, 跳过清除", task_id)
                    return
                logger.debug("clear_heartbeat_if_own: task={} heartbeat 已清除", task_id)
    except SQLAlchemyError:
        logger.exception("heartbeat_clear_failed: task={} 心跳清理失败", task_id)
