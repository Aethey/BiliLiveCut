"""真实 FFmpeg 与本机 HTTP 流验证，无需 B 站账号或外网。"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app.core.config import settings
from app.plugins.live_source import SourceRoom, StreamSpec
from app.recording.recorder import Recorder
from app.recording.stream_race import StreamAttempt, StreamRace


@pytest.mark.parametrize("legacy_progress", [False, True])
@pytest.mark.parametrize("timer_resolution_s", [0.0, 0.05], ids=["normal-clock", "coarse-clock"])
async def test_real_ffmpeg_keeps_winning_http_connection_and_writes_playable_segments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy_progress: bool, timer_resolution_s: float
) -> None:
    binary = shutil.which(settings.ffmpeg_path)
    assert binary is not None, "录制集成测试需要 FFmpeg"
    source = tmp_path / "fixture.ts"
    subprocess.run(
        [
            binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=15",
            "-t",
            "60",
            "-c:v",
            "mpeg2video",
            "-b:v",
            "500k",
            "-minrate",
            "500k",
            "-maxrate",
            "500k",
            "-bufsize",
            "1000k",
            "-g",
            "15",
            "-f",
            "mpegts",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=15,
    )
    media = source.read_bytes()
    requests: list[str] = []
    sent_bytes: dict[str, int] = {}
    progress_reports: dict[str, deque[tuple[float, dict[str, str]]]] = {}
    started = time.monotonic()
    shutdown = threading.Event()
    probes = []
    original_probe = StreamAttempt._has_video_packets

    async def probe(attempt: StreamAttempt) -> bool:
        result = await original_probe(attempt)
        probes.append(result)
        return result

    monkeypatch.setattr(StreamAttempt, "_has_video_packets", probe)
    original_drain = StreamAttempt.drain_progress

    async def observe_progress(attempt: StreamAttempt) -> None:
        assert attempt.process is not None and attempt.process.stdout is not None
        reader = attempt.process.stdout
        original_readline = reader.readline
        reports = progress_reports.setdefault(attempt.stream.url, deque(maxlen=6))
        fields: dict[str, str] = {}

        async def readline() -> bytes:
            while line := await original_readline():
                key, _, value = line.decode("ascii", errors="replace").strip().partition("=")
                if key in {"frame", "out_time_us", "out_time", "progress"}:
                    fields[key] = value
                if key == "progress":
                    reports.append((round(time.monotonic() - started, 3), fields.copy()))
                    fields.clear()
                if not legacy_progress or not line.startswith(b"frame="):
                    return line
            return b""

        monkeypatch.setattr(reader, "readline", readline)
        await original_drain(attempt)

    monkeypatch.setattr(StreamAttempt, "drain_progress", observe_progress)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            requests.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.end_headers()
            try:
                if self.path == "/slow":
                    shutdown.wait(15)  # HTTP 200 但没有媒体，不能胜出。
                    return
                # 连续时间轴，避免重复拼接短 TS 引入倒退时间戳和损坏包。
                stream_started = time.monotonic()
                chunk_size = 1316
                bytes_per_second = chunk_size / 0.01
                for offset in range(0, len(media), chunk_size):
                    self.wfile.write(media[offset : offset + chunk_size])
                    self.wfile.flush()
                    sent_bytes[self.path] = min(offset + chunk_size, len(media))
                    # 按绝对发送时刻补偿唤醒延迟，避免粗定时器把测试流降到媒体码率以下。
                    remaining = stream_started + sent_bytes[self.path] / bytes_per_second - time.monotonic()
                    wait_s = max(remaining, timer_resolution_s) if remaining > 0 else 0.0
                    if shutdown.wait(wait_s):
                        return
                shutdown.wait(15)
            except OSError:
                pass  # 落选连接或测试结束后主动断开。

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    monkeypatch.setattr(settings, "ffmpeg_path", binary)
    monkeypatch.setattr(settings, "segment_duration_s", 5)
    for variable in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(variable, raising=False)
    recorder = Recorder(SourceRoom(platform="external", source_id="1", canonical_url="https://source.invalid/1"), 1)
    output = tmp_path / "race"
    output.mkdir()
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        thread.start()
        candidates = [
            StreamSpec(url=f"http://127.0.0.1:{server.server_port}/{name}", transport="flv", container="flv")
            for name in ("slow", "good")
        ]
        race = StreamRace(
            candidates,
            output,
            0,
            recorder._build_ffmpeg_cmd,
            asyncio.Event(),
            lambda _: None,
            parallel=2,
            timeout_s=10,
            budget_s=12,
        )
        try:
            winner = await asyncio.wait_for(race.run(), 12)
            assert winner is not None and winner.stream.url.endswith("/good"), (
                f"requests={requests}, sent_bytes={sent_bytes}, fixture_bytes={len(media)}, "
                f"progress={progress_reports}, probes={probes}"
            )
            process = winner.process
            assert process is not None and process.returncode is None
            pid = process.pid
            first_size = sum(path.stat().st_size for path in output.glob(f"{winner.prefix}*.ts"))
            for _ in range(30):
                await asyncio.sleep(0.1)
                if sum(path.stat().st_size for path in output.glob(f"{winner.prefix}*.ts")) > first_size:
                    break
            assert process.pid == pid and process.returncode is None
            assert sum(path.stat().st_size for path in output.glob(f"{winner.prefix}*.ts")) > first_size
            assert requests.count("/good") == 1, "胜出线路不应重新连接"
            if legacy_progress:
                assert any(probes), "旧版无 frame 报告必须核验真实本地视频包"
            assert race.attempts[0].process.returncode is not None
            assert not list(output.glob(f"{race.attempts[0].prefix}*"))
            process.stdin.write(b"q\n")
            await process.stdin.drain()
            await asyncio.wait_for(process.wait(), 5)
        finally:
            await race.close()
            shutdown.set()
            server.shutdown()
            thread.join(timeout=3)
    files = list(output.glob("*.ts"))
    assert files
    probe = subprocess.run(
        [
            settings.ffprobe_path,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(files[0]),
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    assert "mpeg2video" in probe.stdout
