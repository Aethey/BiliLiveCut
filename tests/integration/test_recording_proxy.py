"""真实 FFmpeg 的 HTTPS 录制允许连接用户配置的 HTTP 代理。"""

from __future__ import annotations

import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from app.core.config import settings
from app.plugins.live_source import SourceRoom, StreamSpec
from app.recording.recorder import Recorder


def test_https_recording_reaches_http_proxy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """真实命令到达本机 CONNECT 代理；代理受控拒绝，无需外部网络或平台凭据。"""
    requests: list[str] = []

    class ProxyHandler(BaseHTTPRequestHandler):
        def do_CONNECT(self) -> None:  # noqa: N802 — HTTP handler interface
            requests.append(self.path)
            self.send_error(502, "Test proxy deliberately rejects tunnels")

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    binary = shutil.which(settings.ffmpeg_path)
    assert binary is not None, "录制集成测试需要 FFmpeg"
    monkeypatch.setattr(settings, "ffmpeg_path", binary)
    recorder = Recorder(SourceRoom(platform="external", source_id="1", canonical_url="https://source.invalid/1"), 1)
    stream = StreamSpec(url="https://cdn-test.invalid/stream.m3u8", transport="hls", container="ts")
    command = recorder._build_ffmpeg_cmd(stream, tmp_path, "part", tmp_path / "segments.csv")
    with HTTPServer(("127.0.0.1", 0), ProxyHandler) as proxy:
        proxy_url = f"http://127.0.0.1:{proxy.server_port}"
        for variable in ("http_proxy", "HTTP_PROXY"):
            monkeypatch.setenv(variable, proxy_url)
        for variable in ("no_proxy", "NO_PROXY"):
            monkeypatch.setenv(variable, "")
        thread = threading.Thread(target=lambda: proxy.serve_forever(poll_interval=0.01), daemon=True)
        thread.start()
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
        finally:
            proxy.shutdown()
            thread.join(timeout=5)
    assert not thread.is_alive()
    assert requests == ["cdn-test.invalid:443"], result.stderr
    assert result.returncode != 0  # The fixture proxy returns 502 after CONNECT.
    assert "not on whitelist" not in result.stderr
