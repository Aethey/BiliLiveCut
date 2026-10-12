"""任务租约对象。

将 Worker 租约的核心属性封装为不可变数据类, 确保所有阶段函数接收统一租约校验接口。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlmodel import Session, select

from app.db.entities import (
    CandidateStatus,
    HighlightCandidate,
    HighlightEvent,
    RawSegment,
    ReviewStatus,
    SegmentTask,
)


class LeaseLostError(RuntimeError):
    """当前 Worker 已失去任务租约, 结果被丢弃。

    该异常不得标记为普通业务失败, 也不得增加永久失败次数。
    """


@dataclass(frozen=True)
class TaskLease:
    """不可变租约对象。

    任务领取成功后生成, 传入所有阶段函数。
    禁止阶段函数只接收 ``task_id`` 而不接收租约。

    :param task_id: SegmentTask ID。
    :param worker_id: 当前 Worker 唯一标识。
    :param lease_token: 租约令牌 (UUID hex)。
    :param expected_stage: 期望的任务阶段 (如 TRANSCRIBING)。
    """

    task_id: int
    worker_id: str
    lease_token: str
    expected_stage: str

    def __post_init__(self) -> None:
        """验证租约 token 不可为空。"""
        if not self.lease_token:
            raise ValueError("lease_token 不能为空")
        if not self.worker_id:
            raise ValueError("worker_id 不能为空")


def still_owns_lease(db: Session, lease: TaskLease) -> bool:
    """统一租约校验。

    同时验证:
    - task.id == lease.task_id
    - task.claimed_by == lease.worker_id
    - task.lease_token == lease.lease_token
    - task.stage == lease.expected_stage

    禁止只判断 lease_token 相同或任务非 stale。

    :param db: SQLModel Session。
    :param lease: 租约对象。
    :returns: True 表示租约有效。
    """
    task = db.get(SegmentTask, lease.task_id)
    if task is None:
        return False
    return (
        task.claimed_by == lease.worker_id
        and task.lease_token == lease.lease_token
        and task.stage == lease.expected_stage
    )


def approved_task_candidate(db: Session, task: SegmentTask) -> HighlightCandidate:
    """验证任务、片段、候选和已批准事件属于同一来源。"""
    candidate = db.get(HighlightCandidate, task.candidate_id) if task.candidate_id else None
    event = db.get(HighlightEvent, task.event_id) if task.event_id else None
    segment = db.get(RawSegment, task.segment_id)
    event_segment = db.get(RawSegment, event.segment_id) if event and event.segment_id is not None else None
    if (
        candidate is None
        or event is None
        or segment is None
        or candidate.status == CandidateStatus.REJECTED
        or event.review_status not in ReviewStatus.POSITIVE
        or event.candidate_id != candidate.id
        or candidate.session_id != task.session_id
        or event.session_id != task.session_id
        or segment.session_id != task.session_id
        # 合并高光的代表片段可以移动，但仍须属于同一场次。
        or (event.segment_id is not None and (event_segment is None or event_segment.session_id != task.session_id))
    ):
        raise ValueError("任务来源不一致或候选未批准")
    return candidate


def require_current_lease(db: Session, lease: TaskLease) -> None:
    """在新写事务的首次查询前锁定任务并复验租约，阻止人工出片的迟到结果。"""
    if db.get_bind().dialect.name == "sqlite":
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    else:
        db.exec(select(SegmentTask).where(SegmentTask.id == lease.task_id).with_for_update()).first()
    if not still_owns_lease(db, lease):
        raise LeaseLostError(f"结果提交前租约已失效: task={lease.task_id}")
