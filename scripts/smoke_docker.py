"""验证真实 Docker 镜像的认证、依赖、持久化和停止行为。"""

from __future__ import annotations

import argparse
import base64
import json
import os
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

    def start(name: str) -> str:
        containers.append(name)
        network = ["--network", "host"] if args.host_network else ["--publish", "127.0.0.1::8000"]
        command = ["blc", "serve", "--host", "127.0.0.1", "--port", "8000"] if args.host_network else []
        docker(
            "run",
            "--detach",
            "--init",
            "--name",
            name,
            *network,
            "--env",
            "ADMIN_PASSWORD",
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
                if request(url + "/api/dashboard", authenticated=True) == 200:
                    assert request(url + "/api/dashboard") == 401
                    docker("exec", name, "python", "/app/docker/healthcheck.py")
                    return url
            except (OSError, URLError):
                pass
            time.sleep(1)
        raise RuntimeError("容器 Web 服务在 120 秒内没有就绪。")

    try:
        missing = docker("run", "--rm", "--env", "ADMIN_PASSWORD=", args.image, check=False)
        assert missing.returncode == 78 and "ADMIN_PASSWORD" in missing.stderr
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
                assert denied.returncode == 78 and "不可写" in denied.stderr
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
                    "import wave",
                    "from app.accelerators import _c_speedups, _cython_speedups",
                    "assert os.getuid() == 1000 and torch.version.cuda is None",
                    "assert 'DISPLAY' not in os.environ and 'WAYLAND_DISPLAY' not in os.environ",
                    "with wave.open('/data/smoke.wav', 'wb') as audio:",
                    "    audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))",
                    "    audio.writeframes(bytes(32000))",
                    "samples = load_audio_text_image_video('/data/smoke.wav', fs=16000)",
                    "assert isinstance(samples, torch.Tensor) and samples.numel() == 16000",
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
            "color=c=black:s=320x180:d=0.2",
            "-vf",
            "drawtext=font='Noto Sans CJK SC':text='无头测试':fontsize=24:fontcolor=white",
            "-f",
            "null",
            "-",
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
        print("Docker 认证、CPU ASR 导入、原生扩展、FFmpeg、中文字体、持久化和正常停止：通过。")
        return 0
    finally:
        for name in containers:
            docker("rm", "--force", name, check=False)
        docker("volume", "rm", volume, check=False)


if __name__ == "__main__":
    raise SystemExit(main())
