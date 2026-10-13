"""容器启动前检查密码与持久化目录，再交接信号给应用。"""

from __future__ import annotations

import os
import sqlite3
import stat
import string
import sys
import tempfile
from pathlib import Path

from app.core.docker_auth import AuthStore

PASSWORD_GROUPS = (string.ascii_uppercase, string.ascii_lowercase, string.digits, "-_.@")


def read_password(path: Path) -> str | None:
    """只接受完整、私有的已生成密码；损坏时不自动轮换。"""
    try:
        if path.is_symlink():
            raise RuntimeError(f"密码文件不能是符号链接：{path}")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeError(f"无法读取密码文件：{path}；请检查文件权限。") from exc
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(f"密码路径必须为普通文件：{path}")
        if os.name == "posix" and (metadata.st_uid != os.getuid() or metadata.st_mode & 0o077):
            raise RuntimeError(f"密码文件须属于容器用户且权限为 600：{path}")
        raw = stream.read(18).removesuffix(b"\n")
    try:
        password = raw.decode("ascii")
    except UnicodeDecodeError:
        password = ""
    alphabet = "".join(PASSWORD_GROUPS)
    if (
        len(password) != 16
        or any(character not in alphabet for character in password)
        or any(not set(password).intersection(group) for group in PASSWORD_GROUPS)
    ):
        raise RuntimeError(f"密码文件内容无效：{path}；请修复文件或设置 ADMIN_PASSWORD，不会自动重置密码。")
    return password


def prepare_auth(environment: dict[str, str]) -> None:
    """Docker 强制启用网页登录；首次设置需持有只输出在容器日志中的初始化码。"""
    environment["DOCKER_AUTH_ENABLED"] = "true"
    root = Path(environment.get("BLC_APP_ROOT", environment.get("STORAGE_ROOT", "/data"))).resolve()
    store = AuthStore(root)
    legacy_path = root / "config" / "admin-password"
    legacy = None
    configured = environment.get("ADMIN_PASSWORD", "")
    if not configured.strip() and not store.path.exists():
        legacy = read_password(legacy_path)
    try:
        code = store.boot(configured, legacy)
        if legacy is not None:
            # 哈希事务已落盘后才移除旧的明文文件，失败时不假装迁移成功。
            legacy_path.unlink()
    except (OSError, sqlite3.Error) as exc:
        raise RuntimeError("无法初始化认证存储；请检查持久化目录权限和认证库，已有数据不会自动重置。") from exc
    if code:
        print(f"管理员初始化码（30 分钟内有效，仅可使用一次）：{code}", file=sys.stderr, flush=True)
        print("打开 Web 页面设置管理员密码；不要公开容器日志。重启会使未使用的旧码失效。", file=sys.stderr)
    else:
        print("管理员认证已就绪，请打开 Web 页面登录。", file=sys.stderr)


def prepare_environment(environment: dict[str, str]) -> None:
    """检查可写运行目录，并准备 Docker 管理员初始化或登录。"""
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
    prepare_auth(environment)


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
