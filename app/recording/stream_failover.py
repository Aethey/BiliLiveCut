"""会话内的播放线路轮换，不保留签名 URL 或请求头。"""

from __future__ import annotations

from dataclasses import dataclass
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


class StreamFailover:
    """先试未失败线路，再按失败次数与上次失败时间轮换，保留来源的排序偏好。"""

    def __init__(self) -> None:
        self._failures: dict[RouteKey, _Failure] = {}
        self._available: set[RouteKey] = set()

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
        self._available = {route_key(stream) for stream in streams}

        def priority(stream: StreamSpec) -> tuple[int, float]:
            failure = self._failures.get(route_key(stream))
            return (failure.count, failure.last_at) if failure is not None else (0, 0.0)

        return sorted(streams, key=priority)

    def failed(self, stream: StreamSpec, *, now: float) -> None:
        """记录一次断流；全部失败后仍能轮流重试，历史记录数量有界。"""
        key = route_key(stream)
        previous = self._failures.get(key)
        if previous is None and len(self._failures) >= _MAX_FAILED_ROUTES:
            oldest = min(self._failures, key=lambda item: self._failures[item].last_at)
            del self._failures[oldest]
        self._failures[key] = _Failure(count=previous.count + 1 if previous is not None else 1, last_at=now)

    @property
    def has_untried_alternative(self) -> bool:
        """本次地址列表是否还有未失败线路，可免去较长退避等待。"""
        return any(key not in self._failures for key in self._available)
