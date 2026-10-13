"""容器启动检查的失败与命令交接回归。"""

from __future__ import annotations

import importlib.util
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def load_entrypoint():
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
def test_empty_password_generates_private_persistent_token(tmp_path: Path, password: str, capsys) -> None:
    entrypoint = load_entrypoint()
    env = environment(tmp_path / "data")
    env["ADMIN_PASSWORD"] = password
    entrypoint.prepare_environment(env)
    token = env["ADMIN_PASSWORD"]
    path = tmp_path / "data/config/admin-password"
    assert len(token) == 16
    assert all(set(token).intersection(group) for group in entrypoint.PASSWORD_GROUPS)
    assert set(token) <= set("".join(entrypoint.PASSWORD_GROUPS))
    assert path.read_text().strip() == token
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert token not in capsys.readouterr().err
    again = environment(tmp_path / "data")
    again.pop("ADMIN_PASSWORD")
    entrypoint.prepare_environment(again)
    assert again["ADMIN_PASSWORD"] == token


def test_generated_passwords_cover_all_character_groups() -> None:
    entrypoint = load_entrypoint()
    tokens = {entrypoint.generate_password() for _ in range(100)}
    assert len(tokens) == 100
    for token in tokens:
        assert len(token) == 16
        assert all(set(token).intersection(group) for group in entrypoint.PASSWORD_GROUPS)
        assert set(token) <= set("".join(entrypoint.PASSWORD_GROUPS))


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
            entrypoint.ensure_password(env)


def test_shared_password_permissions_are_rejected(tmp_path: Path) -> None:
    entrypoint = load_entrypoint()
    path = tmp_path / "admin-password"
    path.write_text(entrypoint.generate_password())
    metadata = path.stat()
    unsafe_metadata = SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_uid=metadata.st_uid)
    with (
        patch.object(entrypoint.os, "name", "posix"),
        patch.object(entrypoint.os, "getuid", return_value=metadata.st_uid, create=True),
        patch.object(entrypoint.os, "fstat", return_value=unsafe_metadata),
    ):
        with pytest.raises(RuntimeError, match="权限为 600"):
            entrypoint.read_password(path)


def test_concurrent_generation_publishes_one_complete_password(tmp_path: Path) -> None:
    entrypoint = load_entrypoint()

    def generate(_: int) -> str:
        env = environment(tmp_path)
        env["ADMIN_PASSWORD"] = ""
        entrypoint.ensure_password(env)
        return env["ADMIN_PASSWORD"]

    with ThreadPoolExecutor(max_workers=8) as executor:
        tokens = set(executor.map(generate, range(16)))
    assert len(tokens) == 1
    assert list((tmp_path / "config").iterdir()) == [tmp_path / "config/admin-password"]


def test_failed_save_does_not_leave_temporary_password_files(tmp_path: Path) -> None:
    entrypoint = load_entrypoint()
    env = environment(tmp_path)
    env["ADMIN_PASSWORD"] = ""
    with patch.object(entrypoint.os, "link", side_effect=PermissionError("denied")):
        with pytest.raises(RuntimeError, match="无法保存密码文件"):
            entrypoint.ensure_password(env)
    assert list((tmp_path / "config").iterdir()) == []


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
    execute.assert_called_once_with("blc", ["blc", "serve"], env)
    assert marker.read_bytes() == b"existing-data"
    assert (tmp_path / "plugins").is_dir()
    assert (tmp_path / "cache/modelscope").is_dir()


def test_generated_password_is_passed_to_the_application(tmp_path: Path) -> None:
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
    assert passed_environment["ADMIN_PASSWORD"] == (tmp_path / "config/admin-password").read_text().strip()
