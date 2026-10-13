"""容器启动前检查密码与持久化目录，再交接信号给应用。"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def prepare_environment(environment: dict[str, str]) -> None:
    """拒绝空密码及不可写的运行目录，避免服务进入重启循环。"""
    if not environment.get("ADMIN_PASSWORD", "").strip():
        raise RuntimeError("请先在 .env 中设置非空 ADMIN_PASSWORD，再启动 Docker 服务。")

    paths = {
        environment.get("STORAGE_ROOT", "/data"),
        environment.get("PLUGIN_DIR", "/data/plugins"),
        environment.get("BLC_APP_ROOT", "/data"),
        environment.get("HF_HOME", "/data/cache/huggingface"),
        environment.get("MODELSCOPE_CACHE", "/data/cache/modelscope"),
        environment.get("PLAYWRIGHT_BROWSERS_PATH", "/data/cache/playwright"),
    }
    for name in sorted(paths):
        path = Path(name)
        try:
            path.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=path):
                pass
        except OSError as exc:
            raise RuntimeError(f"运行目录不可写：{path}；请为容器用户 UID/GID 1000 配置宿主挂载目录权限。") from exc


def main() -> int:
    """执行启动检查，以 exec 保持应用直接接收容器停止信号。"""
    if len(sys.argv) < 2:
        print("缺少容器启动命令。", file=sys.stderr)
        return 78
    environment = os.environ.copy()
    try:
        prepare_environment(environment)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 78
    os.execvpe(sys.argv[1], sys.argv[1:], environment)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
