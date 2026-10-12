"""Worker 生命周期管理 — 全局状态、子进程跟踪、资源跟踪。"""

from __future__ import annotations

import random
import threading
import time as _time
import uuid
from datetime import UTC, datetime

from app.core.sanitize import safe_exception_summary

_WORKER_ID: str = f"worker-{uuid.uuid4().hex[:8]}"

# 关闭标记 — 使用 threading.Event, 确保所有模块读取同一共享状态
shutdown_event: threading.Event = threading.Event()

# 子进程跟踪 — 锁在模块初始化时立即创建, 避免延迟初始化竞态
_subprocesses: list = []
_subprocesses_lock: threading.Lock = threading.Lock()

# V0.1.13: Task-level resource tracking (ResourceBudget integration)
_task_resources: dict[tuple[int, str | None], dict[str, int | float]] = {}
_task_resources_lock: threading.Lock = threading.Lock()
_executions: set[tuple[int, str]] = set()
_executions_lock = threading.Lock()

_RETRY_JITTER_S: float = 5.0


def register_execution(task_id: int, lease_token: str) -> None:
    """登记本实例仍在执行的租约，取消不能在同步调用收尾前结算。"""
    with _executions_lock:
        _executions.add((task_id, lease_token))


def finish_execution(task_id: int, lease_token: str) -> None:
    """执行及资源清理结束后撤销本地登记，不影响同任务的新租约。"""
    with _executions_lock:
        _executions.discard((task_id, lease_token))


def execution_active(task_id: int, lease_token: str) -> bool:
    """确认原执行是否仍在本实例中，包括尚未返回的同步 LLM/媒体调用。"""
    with _executions_lock:
        return (task_id, lease_token) in _executions


def now_utc() -> datetime:
    """返回 UTC 时间。"""
    return datetime.now(UTC)


def jitter(base: float, jitter_s: float = _RETRY_JITTER_S) -> float:
    """添加随机抖动量。"""
    return base + random.uniform(0, jitter_s)


def track_subprocess(proc) -> None:
    """注册子进程句柄, 供关闭时统一 terminate/kill。"""
    with _subprocesses_lock:
        _subprocesses.append(proc)


def untrack_subprocess(proc) -> None:
    """从跟踪集中移除已正常结束的子进程。"""
    with _subprocesses_lock:
        try:
            _subprocesses.remove(proc)
        except ValueError:
            pass


def cleanup_subprocesses() -> None:
    """关闭所有被跟踪的子进程: SIGTERM → 等待 → SIGKILL。"""
    import logging

    _logger = logging.getLogger(__name__)
    with _subprocesses_lock:
        procs = list(_subprocesses)
        _subprocesses.clear()
    if not procs:
        return
    _logger.warning("清理 %s 个子进程 (SIGTERM)", len(procs))
    for p in procs:
        try:
            if p.poll() is None:
                p.terminate()
        except OSError as exc:
            _logger.error("子进程清理失败 pid=%s: %s", p.pid, safe_exception_summary(exc))
    _time.sleep(5)
    for p in procs:
        try:
            if p.poll() is None:
                p.kill()
                _logger.warning("子进程 %s 已被 SIGKILL", p.pid)
        except OSError as exc:
            _logger.error("子进程清理失败 pid=%s: %s", p.pid, safe_exception_summary(exc))
