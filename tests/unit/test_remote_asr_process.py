"""真实 HTTP 与生产 ASR 子进程之间的完成、复用和取消验证。"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import psutil
import pytest

from app.analysis.transcription.isolated import IsolatedPipeline
from app.analysis.transcription.process_pool import ASRProcessPool
from app.core.config import settings
from app.pipeline.lease import LeaseLostError


@pytest.mark.parametrize("cancel", [False, True])
def test_real_remote_process_reuses_slot_or_stops_blocked_request(
    pcm_wav: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    received = threading.Event()
    release = threading.Event()
    paths: list[str] = []
    uploads: list[bytes] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            paths.append(self.path)
            uploads.append(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.headers["Authorization"] == "Bearer process-fixture-key"
            received.set()
            if cancel:
                assert release.wait(30)
            payload = json.dumps({"text": "远程转写已经通过真实子进程返回。"}).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                assert cancel

    server = HTTPServer(("127.0.0.1", 0), Handler)
    serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    serving.start()
    monkeypatch.setattr(settings, "asr_remote_base_url", f"http://127.0.0.1:{server.server_port}/v1")
    monkeypatch.setattr(settings, "asr_remote_model", "fixture-only-model")
    monkeypatch.setattr(settings, "asr_remote_api_key", "process-fixture-key")
    monkeypatch.setattr(settings, "asr_primary", "openai_compatible")
    monkeypatch.setattr(settings, "asr_fallback_whisper", False)
    monkeypatch.setattr(settings, "asr_remote_timeout_s", 30)
    pool = ASRProcessPool()
    pids: list[int] = []

    def check() -> None:
        if cancel and received.is_set():
            pids.extend(slot.process.pid for slot in pool._slots if slot.process.pid is not None)
            raise LeaseLostError("取消正在等待远程 HTTP 响应的任务")

    try:
        pipeline = IsolatedPipeline(pool=pool, check_cancelled=check)
        if cancel:
            with pytest.raises(LeaseLostError):
                pipeline.transcribe(str(pcm_wav))
            assert pids and all(not psutil.pid_exists(pid) for pid in pids)
            assert not pool._slots and all(value == 0 for value in pool._roles.values())
            assert paths == ["/v1/audio/transcriptions"]
        else:
            result = pipeline.transcribe(str(pcm_wav))
            first_pid = pool._slots[0].process.pid
            again = pipeline.transcribe(str(pcm_wav))
            assert result.text == again.text == "远程转写已经通过真实子进程返回。"
            assert result.backend == "openai_compatible" and result.audio_duration == 8.336
            assert result.segments[0].words == [] and result.segments[0].raw_confidence is None
            assert len(pool._slots) == 1 and pool._slots[0].process.pid == first_pid
            assert paths == ["/v1/audio/transcriptions"] * 2
        assert uploads and all(b'filename="audio.wav"' in upload and b"RIFF" in upload for upload in uploads)
    finally:
        pool.shutdown(0)
        release.set()
        server.shutdown()
        serving.join(timeout=5)
        server.server_close()
        assert not serving.is_alive()
