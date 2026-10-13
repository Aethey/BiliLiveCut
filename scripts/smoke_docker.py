"""验证真实 Docker 镜像的认证、依赖、持久化和停止行为。"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


def main() -> int:
    """使用临时容器和卷执行验收，不下载模型或读取仓库 .env。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="bili-live-cut:docker-test")
    parser.add_argument("--docker", default="docker")
    parser.add_argument("--browser", action="store_true", help="使用 Playwright Chromium 验证实际网页登录")
    parser.add_argument("--host-network", action="store_true", help="供禁用 bridge 的隔离测试 daemon 使用")
    args = parser.parse_args()
    prefix = "blc-docker-smoke-" + secrets.token_hex(5)
    volume = prefix + "-data"
    containers: list[str] = []
    password = secrets.token_urlsafe(24)
    environment = {**os.environ, "ADMIN_PASSWORD": password}

    def docker(*command: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run([args.docker, *command], capture_output=True, text=True, timeout=180, env=environment)
        if check and result.returncode:
            raise RuntimeError(f"Docker {command[0]} 失败：{result.stderr.strip()}")
        return result

    def request(url: str, *, authenticated: bool = False) -> int:
        headers = {}
        if authenticated:
            headers["Authorization"] = "Basic " + base64.b64encode(f"admin:{password}".encode()).decode()
        try:
            with build_opener(ProxyHandler({})).open(Request(url, headers=headers), timeout=3) as response:
                return response.status
        except HTTPError as exc:
            return exc.code

    def start(name: str, *, initialize: bool = False) -> str:
        containers.append(name)
        network = ["--network", "host"] if args.host_network else ["--publish", "127.0.0.1::8000"]
        command = ["blc", "serve", "--host", "127.0.0.1", "--port", "8000"] if args.host_network else []
        docker(
            "run",
            "--detach",
            "--init",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--name",
            name,
            *network,
            "--env",
            "ADMIN_PASSWORD=" if initialize else "ADMIN_PASSWORD",
            "--env",
            "ASR_NO_MODEL_DOWNLOAD=1",
            "--env",
            "TREND_ENABLED=false",
            "--env",
            "TREND_SCHEDULE_ENABLED=false",
            "--env",
            "HTTP_PROXY=http://127.0.0.1:1",
            "--mount",
            f"type=volume,source={volume},target=/data",
            args.image,
            *command,
        )
        port = "8000" if args.host_network else docker("port", name, "8000/tcp").stdout.strip().rsplit(":", 1)[1]
        url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            state = json.loads(docker("inspect", "--format", "{{json .State}}", name).stdout)
            if not state["Running"]:
                logs = docker("logs", name)
                raise RuntimeError(f"容器提前退出：{logs.stdout}{logs.stderr}")
            try:
                if (
                    request(url + ("/auth/status" if initialize else "/api/dashboard"), authenticated=not initialize)
                    == 200
                ):
                    assert request(url + "/api/dashboard") == 401
                    docker("exec", name, "python", "/app/docker/healthcheck.py")
                    return url
            except (OSError, URLError):
                pass
            time.sleep(1)
        raise RuntimeError("容器 Web 服务在 120 秒内没有就绪。")

    try:
        with tempfile.TemporaryDirectory(prefix=prefix) as directory:
            path = Path(directory)
            path.chmod(0o555)
            try:
                denied = docker(
                    "run",
                    "--rm",
                    "--env",
                    "ADMIN_PASSWORD",
                    "--mount",
                    f"type=bind,source={path},target=/data",
                    args.image,
                    check=False,
                )
                assert denied.returncode == 78 and "不可写" in denied.stderr, (denied.returncode, denied.stderr)
            finally:
                path.chmod(0o700)
        docker("volume", "create", volume)
        first = prefix + "-first"
        start(first)
        docker(
            "exec",
            first,
            "python",
            "-c",
            "\n".join(
                [
                    "import os, sqlite3; from pathlib import Path",
                    "import torch, torchaudio; from funasr import AutoModel",
                    "from funasr.utils.load_utils import load_audio_text_image_video",
                    "import wave, av",
                    "from faster_whisper.audio import decode_audio",
                    "assert av.ffmpeg_version_info == '9.0.2'",
                    "status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines())",
                    "assert int(status['NoNewPrivs']) == 1",
                    "assert int(status['CapEff'], 16) == int(status['CapBnd'], 16) == 0",
                    "from app.accelerators import _c_speedups, _cython_speedups",
                    "assert os.getuid() == 1000 and torch.version.cuda is None",
                    "assert 'DISPLAY' not in os.environ and 'WAYLAND_DISPLAY' not in os.environ",
                    "with wave.open('/data/smoke.wav', 'wb') as audio:",
                    "    audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))",
                    "    audio.writeframes(bytes(32000))",
                    "samples = load_audio_text_image_video('/data/smoke.wav', fs=16000)",
                    "assert isinstance(samples, torch.Tensor) and samples.numel() == 16000",
                    "assert decode_audio('/data/smoke.wav', sampling_rate=16000).size == 16000",
                    "db = sqlite3.connect('/data/blc.db')",
                    "assert db.execute(\"select count(*) from sqlite_master where type='table'\").fetchone()[0] > 0",
                    "for key in ('PLUGIN_DIR', 'HF_HOME', 'MODELSCOPE_CACHE'):",
                    "    root = Path(os.environ[key]); assert root.is_relative_to('/data')",
                    "    (root / 'docker-smoke-marker').write_text('persistent')",
                ]
            ),
        )
        filters = docker("exec", first, "ffmpeg", "-hide_banner", "-filters").stdout
        assert "subtitles" in filters and "drawtext" in filters
        fonts = docker("exec", first, "fc-list", ":lang=zh", "family").stdout
        assert "Noto" in fonts
        docker(
            "exec",
            first,
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x180:d=1",
            "-i",
            "/data/smoke.wav",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-vf",
            "drawtext=font='Noto Sans CJK SC':text='无头测试':fontsize=24:fontcolor=white",
            "/data/smoke.mp4",
        )
        docker(
            "exec",
            first,
            "python",
            "-c",
            "from faster_whisper.audio import decode_audio; "
            "assert decode_audio('/data/smoke.mp4', sampling_rate=16000).size >= 16000",
        )
        docker("stop", "--time", "60", first)
        state = json.loads(docker("inspect", "--format", "{{json .State}}", first).stdout)
        assert state["ExitCode"] in (0, 143) and not state["OOMKilled"]
        logs = docker("logs", first)
        # Uvicorn 重放 SIGTERM 时最后一条异步日志未必已刷出；退出状态排除强制杀死。
        assert "Web 服务关闭失败" not in logs.stdout + logs.stderr
        second = prefix + "-second"
        start(second)
        docker(
            "exec",
            second,
            "python",
            "-c",
            "\n".join(
                [
                    "import os; from pathlib import Path",
                    "for key in ('PLUGIN_DIR', 'HF_HOME', 'MODELSCOPE_CACHE'):",
                    "    assert (Path(os.environ[key]) / 'docker-smoke-marker').read_text() == 'persistent'",
                ]
            ),
        )
        docker("stop", "--time", "60", second)
        third = prefix + "-initialized"
        url = start(third, initialize=True)
        logs = docker("logs", third)
        code = re.findall(r"仅可使用一次）：([\w-]+)", logs.stdout + logs.stderr)[-1]
        password = secrets.token_urlsafe(24)

        def expire_session() -> None:
            docker(
                "exec",
                third,
                "python",
                "-c",
                "import sqlite3; db=sqlite3.connect('/data/config/web-auth.sqlite3'); "
                "db.execute('UPDATE sessions SET seen=0'); db.commit(); db.close()",
            )

        if args.browser:
            from check_docker_auth_browser import check_browser

            password = check_browser(url, code, password, expire_session)
        else:
            payload = json.dumps({"code": code, "password": password}).encode()
            with build_opener(ProxyHandler({})).open(
                Request(url + "/auth/setup", data=payload, headers={"Content-Type": "application/json", "Origin": url}),
                timeout=10,
            ) as result:
                assert result.status == 200
        assert request(url + "/api/dashboard", authenticated=True) == 200
        docker(
            "exec",
            third,
            "python",
            "-c",
            "import os, stat; from pathlib import Path; "
            "s = Path('/data/config/web-auth.sqlite3').stat(); "
            "assert stat.S_IMODE(s.st_mode) == 0o600 and s.st_uid == os.getuid()",
        )
        logs = docker("logs", third)
        assert password not in logs.stdout + logs.stderr
        docker("stop", "--time", "60", third)
        fourth = prefix + "-initialized-recreated"
        url = start(fourth, initialize=True)
        assert request(url + "/api/dashboard", authenticated=True) == 200
        logs = docker("logs", fourth)
        assert "仅可使用一次）" not in logs.stdout + logs.stderr
        docker("stop", "--time", "60", fourth)
        print("Docker 初始化、密码持久化、CPU ASR、原生扩展、FFmpeg、字体、数据持久化和正常停止：通过。")
        return 0
    finally:
        for name in containers:
            docker("rm", "--force", name, check=False)
        docker("volume", "rm", volume, check=False)


if __name__ == "__main__":
    raise SystemExit(main())
