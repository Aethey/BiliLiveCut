"""会话内的播放线路轮换，不保留签名 URL 或请求头。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from app.plugins.live_source import StreamSpec

# 同一主机的不同播放格式可能有不同可达性；签名、路径变化不算新线路。
RouteKey = tuple[str, str, int | None, str, str, str | None, str | None]
_FAILURE_TTL_S = 300.0
_MAX_FAILED_ROUTES = 256


def route_key(stream: StreamSpec) -> RouteKey:
    """按服务器、协议、格式、编码和画质识别线路，忽略临时凭据。"""
    parsed = urlsplit(stream.url)
    return (
        parsed.scheme.lower(),
        (parsed.hostname or "").lower(),
        parsed.port or (443 if parsed.scheme.lower() == "https" else 80),
        stream.transport,
        stream.container,
        stream.codec,
        stream.quality_id,
    )


@dataclass(slots=True)
class _Failure:
    count: int
    last_at: float
    next_slot: int = 0
    next_candidate: bytes | None = None
    tried_slots: set[int] = field(default_factory=set)


def _candidate_key(stream: StreamSpec) -> bytes:
    """区分本轮完整端点，仅保存摘要，不把播放路径或凭据留在失败记录中。"""
    value = (stream.url, sorted((name.lower(), value) for name, value in stream.headers.items()))
    return hashlib.sha256(json.dumps(value, ensure_ascii=True).encode("utf-8")).digest()


class StreamFailover:
    """先试未失败线路，再按失败次数与上次失败时间轮换，保留来源的排序偏好。"""

    def __init__(self) -> None:
        self._failures: dict[RouteKey, _Failure] = {}
        self._candidates: dict[RouteKey, dict[bytes, int]] = {}

    def reset(self) -> None:
        """新场次或稳定产出后清除历史惩罚，避免永久排除恢复的节点。"""
        self._failures.clear()

    def choose(self, streams: list[StreamSpec], *, now: float) -> StreamSpec | None:
        """始终从本次刷新后的地址中选择，不缓存可能已过期的播放 URL。"""
        ordered = self.ranked(streams, now=now)
        return ordered[0] if ordered else None

    def ranked(self, streams: list[StreamSpec], *, now: float) -> list[StreamSpec]:
        """按短期失败记录排序，供串行回退与并发竞争共用。"""
        self._failures = {
            key: failure for key, failure in self._failures.items() if now - failure.last_at < _FAILURE_TTL_S
        }
        candidates: dict[RouteKey, dict[bytes, int]] = {}
        keys = [(stream, route_key(stream), _candidate_key(stream)) for stream in streams]
        for _, route, candidate in keys:
            slots = candidates.setdefault(route, {})
            slots.setdefault(candidate, len(slots))
        for route, slots in candidates.items():
            failure = self._failures.get(route)
            if failure is not None:
                # 完整端点未变时跟随端点；签名全部刷新时沿用来源列表中的轮换位置。
                old_slots = self._candidates.get(route, {})
                failure.tried_slots = {
                    slots.get(key, slot)
                    for key, slot in old_slots.items()
                    if slot in failure.tried_slots and slots.get(key, slot) < len(slots)
                }
        self._candidates = candidates

        def priority(item: tuple[StreamSpec, RouteKey, bytes]) -> tuple[int, float, int]:
            _, route, candidate = item
            failure = self._failures.get(route)
            if failure is None:
                return (0, 0.0, 0)
            slots = candidates[route]
            start = (
                slots.get(failure.next_candidate, failure.next_slot) if failure.next_candidate else failure.next_slot
            )
            return (failure.count, failure.last_at, (slots[candidate] - start) % len(slots))

        return [stream for stream, _, _ in sorted(keys, key=priority)]

    def failed(self, stream: StreamSpec, *, now: float) -> None:
        """记录一次断流；全部失败后仍能轮流重试，历史记录数量有界。"""
        key = route_key(stream)
        previous = self._failures.get(key)
        if previous is None and len(self._failures) >= _MAX_FAILED_ROUTES:
            oldest = min(self._failures, key=lambda item: self._failures[item].last_at)
            del self._failures[oldest]
        failure = previous or _Failure(count=0, last_at=now)
        failure.count += 1
        failure.last_at = now
        slots = self._candidates.get(key, {})
        slot = slots.get(_candidate_key(stream))
        if slot is not None:
            failure.tried_slots.add(slot)
            failure.next_slot = (slot + 1) % len(slots)
            failure.next_candidate = next(candidate for candidate, index in slots.items() if index == failure.next_slot)
        self._failures[key] = failure

    @property
    def has_untried_alternative(self) -> bool:
        """本次地址列表是否还有未失败线路，可免去较长退避等待。"""
        return any(
            route not in self._failures or len(self._failures[route].tried_slots) < len(slots)
            for route, slots in self._candidates.items()
        )
