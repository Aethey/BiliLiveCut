"""纯 ASR 执行的阶段事件，可由隔离进程转发给任务监管器。"""

from __future__ import annotations

from collections.abc import Callable

from loguru import logger

_sink: Callable[[str, dict[str, object]], None] | None = None


def set_progress_sink(sink: Callable[[str, dict[str, object]], None] | None) -> None:
    """设置单请求子进程的事件出口，不使用 SDK 的 stdout 作为协议。"""
    global _sink
    _sink = sink


def report_progress(stage: str, **details: object) -> None:
    """发送阶段及模型上下文，保留本地可读日志。"""
    logger.info("asr_stage stage={} details={}", stage, details)
    if _sink is not None:
        _sink(stage, details)
