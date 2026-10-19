from __future__ import annotations

import pytest

from app.core.ffmpeg_errors import FfmpegErrorType, classify_ffmpeg_error
from app.plugins.live_source import StreamSpec
from app.recording.stream_failover import StreamFailover, route_key


def stream(host: str, **kwargs: object) -> StreamSpec:
    return StreamSpec(url=f"https://{host}/live?token=private", transport="hls", container="ts", **kwargs)


def test_refresh_and_reordering_do_not_retry_failed_route() -> None:
    selector = StreamFailover()
    first, second = stream("first.invalid"), stream("second.invalid")
    assert selector.choose([first, second], now=0) is first
    selector.failed(first, now=1)
    refreshed = first.model_copy(update={"url": "https://first.invalid/new-signed-path?token=new-private"})
    assert selector.choose([refreshed, second], now=2) is second
    selector.failed(second, now=3)
    assert not selector.has_untried_alternative
    # 所有线路都失败后，先重试较早失败的线路，且必须用最新签名。
    assert selector.choose([second, refreshed], now=4) is refreshed
    selector.failed(refreshed, now=5)
    assert selector.choose([refreshed, second], now=6) is second


def test_formats_codecs_and_quality_are_distinct_routes_on_same_host() -> None:
    first = stream("cdn.invalid", codec="avc", quality_id="10000")
    variants = [
        first,
        first.model_copy(update={"container": "fmp4"}),
        first.model_copy(update={"transport": "flv", "container": "flv"}),
        first.model_copy(update={"codec": "hevc"}),
        first.model_copy(update={"quality_id": "400"}),
    ]
    selector = StreamFailover()
    for index, expected in enumerate(variants):
        assert selector.choose(variants, now=index) is expected
        selector.failed(expected, now=index)
        assert selector.has_untried_alternative == (index < len(variants) - 1)


def test_penalties_expire_and_reset_and_new_candidates_take_precedence() -> None:
    first, second, third = (stream(f"cdn{index}.invalid") for index in range(3))
    selector = StreamFailover()
    selector.choose([first, second], now=0)
    selector.failed(first, now=1)
    selector.failed(second, now=2)
    assert selector.choose([first, second, third], now=3) is third
    assert selector.choose([first, second], now=301) is first
    selector.failed(first, now=302)
    assert selector.choose([first, second], now=303) is second
    selector.reset()
    assert selector.choose([first, second], now=304) is first
    assert selector.choose([], now=305) is None
    assert not selector.has_untried_alternative


def test_default_port_and_case_identify_same_route_without_credentials() -> None:
    first = stream("CDN.invalid")
    second = first.model_copy(update={"url": "https://cdn.invalid:443/other?signature=new"})
    assert route_key(first) == route_key(second)
    assert "private" not in repr(route_key(first))


@pytest.mark.parametrize("difference", ["path", "query", "headers"])
def test_same_route_rotates_distinct_endpoints_without_retrying_duplicates(difference: str) -> None:
    first = stream("cdn.invalid")
    updates = {
        "path": {"url": "https://cdn.invalid/backup?token=private"},
        "query": {"url": "https://cdn.invalid/live?token=other-private"},
        "headers": {"headers": {"X-Key": "private-header"}},
    }
    second = first.model_copy(update=updates[difference])
    candidates = [first, first.model_copy(), second]
    selector = StreamFailover()
    assert selector.choose(candidates, now=0) is first
    selector.failed(first, now=1)
    # 能力探测失败和串行录制失败可能重复报告同一端点，不能跳过备用端点。
    selector.failed(first, now=2)
    assert selector.has_untried_alternative
    assert selector.choose(candidates, now=3) is second
    selector.failed(second, now=4)
    assert not selector.has_untried_alternative
    assert selector.choose(candidates, now=5) is first
    assert "private" not in repr(vars(selector))


def test_refreshing_all_paths_and_credentials_preserves_round_robin_position() -> None:
    selector = StreamFailover()
    for attempt in range(7):
        candidates = [
            StreamSpec(
                url=f"https://cdn.invalid/signed-{attempt}/{slot}?token=private-{attempt}",
                headers={"X-Key": f"private-header-{attempt}"},
                transport="hls",
                container="ts",
            )
            for slot in range(3)
        ]
        chosen = selector.choose(candidates, now=attempt)
        assert chosen is candidates[attempt % 3]
        selector.failed(chosen, now=attempt)
        assert selector.has_untried_alternative == (attempt < 2)
    assert "private" not in repr(vars(selector))


def test_reordering_existing_candidates_keeps_the_next_untried_endpoint() -> None:
    first = stream("cdn.invalid")
    second = first.model_copy(update={"url": "https://cdn.invalid/backup"})
    selector = StreamFailover()
    assert selector.choose([first, second], now=0) is first
    selector.failed(first, now=1)
    assert selector.choose([second, first], now=2) is second
    assert selector.has_untried_alternative
    selector.failed(second, now=3)
    assert not selector.has_untried_alternative
    assert selector.choose([second, first], now=4) is first


def test_candidate_removal_expiry_and_reset_do_not_leave_an_invalid_cursor() -> None:
    first = stream("cdn.invalid")
    second = first.model_copy(update={"url": "https://cdn.invalid/backup"})
    selector = StreamFailover()
    selector.choose([first, second], now=0)
    selector.failed(first, now=1)
    assert selector.choose([first], now=2) is first
    selector.failed(first, now=3)
    assert not selector.has_untried_alternative
    assert selector.choose([], now=4) is None
    assert not selector.has_untried_alternative
    assert selector.choose([first, second], now=303) is first
    selector.failed(first, now=304)
    assert selector.choose([first, second], now=305) is second
    selector.reset()
    assert selector.choose([first, second], now=306) is first
    assert selector.has_untried_alternative


@pytest.mark.parametrize(
    "stderr,expected",
    [
        ("[tls @ 0001] Unable to read from socket", FfmpegErrorType.TRANSIENT_NETWORK),
        ("[https @ 0001] Error reading HTTP response: Error number -138 occurred", FfmpegErrorType.TRANSIENT_NETWORK),
        ("[tcp @ 0001] Error number -138 occurred", FfmpegErrorType.TRANSIENT_NETWORK),
        ("[in#0 @ 0001] Error number -138 occurred", FfmpegErrorType.UNKNOWN),
        ("[tls @ 0001] Unable to read from socket\nNo space left on device", FfmpegErrorType.DISK_FULL),
    ],
)
def test_mingw_socket_timeout_classification(stderr: str, expected: FfmpegErrorType) -> None:
    assert classify_ffmpeg_error(4294967158, stderr) is expected
