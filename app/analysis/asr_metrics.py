"""ASR 可观测性指标收集。

收集各后端的调用、耗时、复核、fallback 和资源指标,
供运维页面和日志使用。
"""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class BackendStats:
    """单个后端的统计。"""

    calls: int = 0
    successes: int = 0
    failures: int = 0
    total_duration: float = 0.0
    p50_duration: float = 0.0
    p95_duration: float = 0.0
    _durations: list[float] = field(default_factory=list)

    @property
    def avg_duration(self) -> float:
        """平均耗时 (秒)。"""
        return self.total_duration / self.calls if self.calls > 0 else 0.0

    @property
    def failure_rate(self) -> float:
        """调用失败率。"""
        return self.failures / self.calls if self.calls > 0 else 0.0

    def record(self, duration: float, success: bool = True) -> None:
        """记录一次调用,更新计数、总耗时和百分位样本。"""
        self.calls += 1
        if success:
            self.successes += 1
        else:
            self.failures += 1
        self.total_duration += duration
        self._durations.append(duration)
        if len(self._durations) > 1000:
            self._durations = self._durations[-500:]

    def recompute_percentiles(self) -> None:
        """从已收集的耗时样本重新计算 P50/P95 百分位数。"""
        if not self._durations:
            return
        sorted_d = sorted(self._durations)
        n = len(sorted_d)
        self.p50_duration = sorted_d[n // 2]
        self.p95_duration = sorted_d[int(n * 0.95)]


@dataclass
class ReviewStats:
    """复核统计。"""

    triggered: int = 0
    succeeded: int = 0
    failed: int = 0
    text_modified: int = 0  # final != base
    kept_base: int = 0  # 保留基础文本
    adopted_review: int = 0  # 采用复核文本
    manual_needed: int = 0  # 标记人工确认

    @property
    def trigger_rate(self) -> float:
        """复核触发率 (复核数 / Paraformer 调用数)。"""
        stats = _backend_stats.get("paraformer")
        total = stats.calls if stats else 0
        return self.triggered / total if total > 0 else 0.0

    @property
    def adoption_rate(self) -> float:
        """复核文本采纳率 (采纳复核 / 触发复核)。"""
        return self.adopted_review / self.triggered if self.triggered > 0 else 0.0


_backend_stats: dict[str, BackendStats] = defaultdict(BackendStats)
_review_stats = ReviewStats()
_oom_count: int = 0
_fallback_count: int = 0
_rtf_samples: list[float] = []
_lock = threading.Lock()
_event_sink: Callable[[str, dict[str, Any]], None] | None = None


def set_event_sink(sink: Callable[[str, dict[str, Any]], None] | None) -> None:
    """子进程逐事件转发指标；不重复合并累计快照。"""
    global _event_sink
    _event_sink = sink


def apply_event(event: str, values: dict[str, Any]) -> None:
    """只接受固定的指标事件类型。JSON 字典仅用于 IPC 边界。"""
    handlers = {
        "backend_call": record_backend_call,
        "review": record_review,
        "review_success": record_review_success,
        "review_failure": record_review_failure,
        "fallback": record_fallback,
        "oom": record_oom,
        "rtf": record_rtf,
    }
    if event not in handlers:
        raise ValueError(f"未知 ASR 指标事件: {event}")
    handlers[event](**values)


def _emit(event: str, **values: Any) -> None:
    if _event_sink is not None:
        _event_sink(event, values)


def record_backend_call(backend: str, duration: float, success: bool = True) -> None:
    """记录一次后端调用。

    :param backend: 后端名 (paraformer/whisper/funasr-nano/sensevoice)。
    :param duration: 耗时秒。
    :param success: 是否成功。
    """
    _emit("backend_call", backend=backend, duration=duration, success=success)
    with _lock:
        _backend_stats[backend].record(duration, success)
        _backend_stats[backend].recompute_percentiles()


def record_review(adopted: bool, kept_base: bool, manual_needed: bool) -> None:
    """记录一次复核结果。"""
    _emit("review", adopted=adopted, kept_base=kept_base, manual_needed=manual_needed)
    with _lock:
        _review_stats.triggered += 1
        if adopted:
            _review_stats.adopted_review += 1
        if kept_base:
            _review_stats.kept_base += 1
        if manual_needed:
            _review_stats.manual_needed += 1


def record_review_success() -> None:
    """记录一次复核成功。"""
    _emit("review_success")
    with _lock:
        _review_stats.succeeded += 1


def record_review_failure() -> None:
    """记录一次复核失败。"""
    _emit("review_failure")
    with _lock:
        _review_stats.failed += 1


def record_fallback() -> None:
    """记录一次 Whisper fallback (V0.1.14.11: 线程安全的 fallback 计数)。"""
    global _fallback_count
    _emit("fallback")
    with _lock:
        _fallback_count += 1


def record_oom() -> None:
    """记录一次 OOM。"""
    global _oom_count
    _emit("oom")
    with _lock:
        _oom_count += 1


def record_rtf(rtf: float) -> None:
    """记录一次 RTF。"""
    _emit("rtf", rtf=rtf)
    with _lock:
        _rtf_samples.append(rtf)
        if len(_rtf_samples) > 1000:
            _rtf_samples[:] = _rtf_samples[-500:]


def get_snapshot() -> dict:
    """获取当前指标快照 (供运维页面)。"""
    with _lock:
        backends = {}
        for name, stats in _backend_stats.items():
            backends[name] = {
                "calls": stats.calls,
                "successes": stats.successes,
                "failures": stats.failures,
                "failure_rate": round(stats.failure_rate, 4),
                "avg_duration": round(stats.avg_duration, 3),
                "p50_duration": round(stats.p50_duration, 3),
                "p95_duration": round(stats.p95_duration, 3),
            }
        rtf_avg = sum(_rtf_samples) / len(_rtf_samples) if _rtf_samples else 0.0
        rtf_sorted = sorted(_rtf_samples) if _rtf_samples else [0.0]
        rtp_p95 = rtf_sorted[int(len(rtf_sorted) * 0.95)] if len(rtf_sorted) > 1 else rtf_avg

        return {
            "backends": backends,
            "review": {
                "triggered": _review_stats.triggered,
                "succeeded": _review_stats.succeeded,
                "failed": _review_stats.failed,
                "text_modified": _review_stats.text_modified,
                "kept_base": _review_stats.kept_base,
                "adopted_review": _review_stats.adopted_review,
                "manual_needed": _review_stats.manual_needed,
                "trigger_rate": round(_review_stats.trigger_rate, 4),
                "adoption_rate": round(_review_stats.adoption_rate, 4),
            },
            "oom_count": _oom_count,
            "fallback_count": _fallback_count,
            "rtf_avg": round(rtf_avg, 4),
            "rtf_p95": round(rtp_p95, 4),
            "rtf_samples": len(_rtf_samples),
        }
