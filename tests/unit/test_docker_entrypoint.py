"""容器启动检查的失败与命令交接回归。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
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
def test_empty_password_fails_before_directory_creation(tmp_path: Path, password: str) -> None:
    entrypoint = load_entrypoint()
    env = environment(tmp_path / "data")
    env["ADMIN_PASSWORD"] = password
    with pytest.raises(RuntimeError, match="ADMIN_PASSWORD"):
        entrypoint.prepare_environment(env)
    assert not (tmp_path / "data").exists()


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
