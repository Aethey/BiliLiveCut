"""容器启动检查的失败与命令交接回归。"""

from __future__ import annotations

import importlib.util
import os
import re
import stat
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from app.core.docker_auth import AuthError, AuthStore

if TYPE_CHECKING:
    from pytest import CaptureFixture


def load_entrypoint() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "packaging/docker/entrypoint.py"
    spec = importlib.util.spec_from_file_location("docker_entrypoint", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def environment(tmp_path: Path) -> dict[str, str]:
    return {
        "ADMIN_PASSWORD": "test-administrator-password",
        "STORAGE_ROOT": str(tmp_path),
        "PLUGIN_DIR": str(tmp_path / "plugins"),
        "BLC_APP_ROOT": str(tmp_path),
        "HF_HOME": str(tmp_path / "cache/huggingface"),
        "MODELSCOPE_CACHE": str(tmp_path / "cache/modelscope"),
        "PLAYWRIGHT_BROWSERS_PATH": str(tmp_path / "cache/playwright"),
    }


@pytest.mark.parametrize("password", ["", " ", "\t"])
def test_empty_password_requires_one_time_setup(tmp_path: Path, password: str, capsys: CaptureFixture[str]) -> None:
    entrypoint = load_entrypoint()
    env = environment(tmp_path / "data")
    env["ADMIN_PASSWORD"] = password
    entrypoint.prepare_environment(env)
    assert env["DOCKER_AUTH_ENABLED"] == "true"
    assert env["ADMIN_PASSWORD"] == password
    store = AuthStore(tmp_path / "data")
    code = re.search(r"仅可使用一次）：([\w-]+)", capsys.readouterr().err).group(1)
    assert store.state()["setup_required"]
    assert code.encode() not in store.path.read_bytes()
    store.initialize(code, "my-first-safe-password", "127.0.0.1")
    again = environment(tmp_path / "data")
    again.pop("ADMIN_PASSWORD")
    entrypoint.prepare_environment(again)
    assert not store.state()["setup_required"]
    assert store.authenticate("admin", "my-first-safe-password", {}, "127.0.0.1")
    assert "my-first-safe-password" not in capsys.readouterr().err
    assert not (tmp_path / "data/config/admin-password").exists()
    if os.name == "posix":
        assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


def test_legacy_password_is_migrated_once_without_changing_login(tmp_path: Path) -> None:
    entrypoint = load_entrypoint()
    env = environment(tmp_path)
    env["ADMIN_PASSWORD"] = ""
    legacy = tmp_path / "config/admin-password"
    legacy.parent.mkdir()
    legacy.write_text("Abcdefghijk1234_")
    legacy.chmod(0o600)
    entrypoint.prepare_environment(env)
    store = AuthStore(tmp_path)
    assert not legacy.exists()
    assert b"Abcdefghijk1234_" not in store.path.read_bytes()
    assert store.authenticate("admin", "Abcdefghijk1234_", {}, "local")


def test_custom_password_does_not_read_or_create_default_file(tmp_path: Path) -> None:
    entrypoint = load_entrypoint()
    env = environment(tmp_path)
    path = tmp_path / "config/admin-password"
    path.parent.mkdir()
    path.write_text("invalid-old-file")
    entrypoint.prepare_environment(env)
    assert env["ADMIN_PASSWORD"] == "test-administrator-password"
    assert path.read_text() == "invalid-old-file"


@pytest.mark.parametrize("contents", ["", "short", "A" * 16, "Abcdefghijk1234!", "Abcdefghijk1234_\nextra"])
def test_invalid_password_file_is_not_silently_rotated(tmp_path: Path, contents: str) -> None:
    entrypoint = load_entrypoint()
    env = environment(tmp_path)
    env["ADMIN_PASSWORD"] = ""
    path = tmp_path / "config/admin-password"
    path.parent.mkdir()
    path.write_text(contents)
    path.chmod(0o600)
    with pytest.raises(RuntimeError, match="内容无效"):
        entrypoint.prepare_environment(env)
    assert path.read_text() == contents


def test_symlink_password_file_is_rejected(tmp_path: Path) -> None:
    entrypoint = load_entrypoint()
    env = environment(tmp_path)
    env["ADMIN_PASSWORD"] = ""
    with patch.object(entrypoint.Path, "is_symlink", return_value=True):
        with pytest.raises(RuntimeError, match="符号链接"):
            entrypoint.prepare_auth(env)


def test_shared_password_permissions_are_rejected(tmp_path: Path) -> None:
    entrypoint = load_entrypoint()
    path = tmp_path / "admin-password"
    path.write_text("Abcdefghijk1234_")
    metadata = path.stat()
    unsafe_metadata = SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_uid=metadata.st_uid)
    with (
        patch.object(entrypoint.os, "name", "posix"),
        patch.object(entrypoint.os, "getuid", return_value=metadata.st_uid, create=True),
        patch.object(entrypoint.os, "fstat", return_value=unsafe_metadata),
    ):
        with pytest.raises(RuntimeError, match="权限为 600"):
            entrypoint.read_password(path)


def test_auth_storage_failure_does_not_start_without_protection(tmp_path: Path) -> None:
    entrypoint = load_entrypoint()
    env = environment(tmp_path)
    env["ADMIN_PASSWORD"] = ""
    with patch.object(entrypoint.AuthStore, "boot", side_effect=PermissionError("denied")):
        with pytest.raises(RuntimeError, match="无法初始化认证存储"):
            entrypoint.prepare_auth(env)


def test_read_only_directory_has_actionable_error(tmp_path: Path) -> None:
    entrypoint = load_entrypoint()
    with patch.object(entrypoint.tempfile, "TemporaryFile", side_effect=PermissionError("denied")):
        with pytest.raises(RuntimeError, match="不可写.*1000"):
            entrypoint.prepare_environment(environment(tmp_path))


def test_existing_data_is_preserved_and_command_is_execed(tmp_path: Path) -> None:
    entrypoint = load_entrypoint()
    env = environment(tmp_path)
    marker = tmp_path / "blc.db"
    marker.write_bytes(b"existing-data")
    with (
        patch.dict(entrypoint.os.environ, env, clear=True),
        patch.object(entrypoint.sys, "argv", ["entrypoint.py", "blc", "serve"]),
        patch.object(entrypoint.os, "execvpe") as execute,
    ):
        assert entrypoint.main() == 0
    execute.assert_called_once_with("blc", ["blc", "serve"], {**env, "DOCKER_AUTH_ENABLED": "true"})
    assert marker.read_bytes() == b"existing-data"
    assert (tmp_path / "plugins").is_dir()
    assert (tmp_path / "cache/modelscope").is_dir()


def test_initialization_does_not_pass_a_plaintext_default_password(tmp_path: Path) -> None:
    entrypoint = load_entrypoint()
    env = environment(tmp_path)
    env["ADMIN_PASSWORD"] = ""
    with (
        patch.dict(entrypoint.os.environ, env, clear=True),
        patch.object(entrypoint.sys, "argv", ["entrypoint.py", "blc", "serve"]),
        patch.object(entrypoint.os, "execvpe") as execute,
    ):
        assert entrypoint.main() == 0
    passed_environment = execute.call_args.args[2]
    assert passed_environment["ADMIN_PASSWORD"] == ""
    assert passed_environment["DOCKER_AUTH_ENABLED"] == "true"


def test_restart_rotates_unused_setup_code(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    entrypoint = load_entrypoint()
    env = environment(tmp_path)
    env["ADMIN_PASSWORD"] = ""
    entrypoint.prepare_environment(env)
    first = re.search(r"仅可使用一次）：([\w-]+)", capsys.readouterr().err).group(1)
    entrypoint.prepare_environment(env)
    second = re.search(r"仅可使用一次）：([\w-]+)", capsys.readouterr().err).group(1)
    assert first != second
    store = AuthStore(tmp_path)
    with pytest.raises(AuthError, match="无效或已过期"):
        store.initialize(first, "my-first-safe-password", "local")
    store.initialize(second, "my-first-safe-password", "local")
