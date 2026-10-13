"""容器启动前检查密码与持久化目录，再交接信号给应用。"""

from __future__ import annotations

import os
import secrets
import stat
import string
import sys
import tempfile
from pathlib import Path

PASSWORD_GROUPS = (string.ascii_uppercase, string.ascii_lowercase, string.digits, "-_.@")


def generate_password() -> str:
    """使用系统随机源，保证 16 位密码包含四类字符。"""
    alphabet = "".join(PASSWORD_GROUPS)
    characters = [secrets.choice(group) for group in PASSWORD_GROUPS]
    characters.extend(secrets.choice(alphabet) for _ in range(16 - len(characters)))
    secrets.SystemRandom().shuffle(characters)
    return "".join(characters)


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


def ensure_password(environment: dict[str, str]) -> None:
    """显式密码优先，否则原子保存并复用持久化默认密码。"""
    if environment.get("ADMIN_PASSWORD", "").strip():
        return
    root = Path(environment.get("BLC_APP_ROOT", environment.get("STORAGE_ROOT", "/data")))
    path = root / "config/admin-password"
    temporary: Path | None = None
    try:
        password = read_password(path)
        if password is None:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="ascii", newline="\n", dir=path.parent, delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(generate_password() + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            try:
                # 硬链接发布完整文件，避免并发启动覆盖密码或读到半写入内容。
                os.link(temporary, path)
                print(f"已生成管理员密码，保存于 {path}；请从容器内读取该文件。", file=sys.stderr)
            except FileExistsError:
                pass
            password = read_password(path)
        if password is None:
            raise RuntimeError(f"密码文件在启动期间消失：{path}；请重试。")
        environment["ADMIN_PASSWORD"] = password
    except OSError as exc:
        raise RuntimeError(f"无法保存密码文件：{path}；请检查持久化目录权限。") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def prepare_environment(environment: dict[str, str]) -> None:
    """检查可写运行目录，并为未配置密码的部署提供持久化随机密码。"""
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
    ensure_password(environment)


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
