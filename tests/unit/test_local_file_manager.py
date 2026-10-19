"""设置中心本地文件管理：目录白名单与打开错误处理。"""

from __future__ import annotations

import pytest

from app.web.services import local_files


@pytest.fixture
def isolated_folders(tmp_path, monkeypatch):
    root = tmp_path / "storage"
    plugins = tmp_path / "external_plugins"
    monkeypatch.setattr(local_files.settings, "storage_root", str(root))
    monkeypatch.setattr(local_files.settings, "plugin_dir", str(plugins))
    return root, plugins


def test_local_folder_list_uses_configured_directories_without_creating(isolated_folders):
    root, plugins = isolated_folders

    folders = {item["id"]: item for item in local_files.list_local_folders()}

    assert set(folders) == {"storage", "raw", "clips", "ready_to_upload", "logs", "plugins"}
    assert folders["storage"]["path"] == str(root)
    assert folders["raw"]["path"] == str(root / "raw")
    assert folders["plugins"]["path"] == str(plugins)
    assert all(not folder["exists"] for folder in folders.values())
    assert not root.exists()
    assert not plugins.exists()


@pytest.mark.parametrize("folder_id", ["storage", "raw", "clips", "ready_to_upload", "logs", "plugins"])
def test_local_folder_open_creates_selected_directory(isolated_folders, monkeypatch, folder_id):
    root, plugins = isolated_folders
    opened = []
    monkeypatch.setattr(local_files, "open_path", lambda path: opened.append(path) or True)

    result = local_files.open_local_folder(folder_id)

    expected = plugins if folder_id == "plugins" else root / folder_id if folder_id != "storage" else root
    assert opened == [expected]
    assert expected.is_dir()
    assert result == {"id": folder_id, "path": str(expected)}


def test_local_folder_rejects_arbitrary_paths(isolated_folders, monkeypatch):
    opened = []
    monkeypatch.setattr(local_files, "open_path", lambda path: opened.append(path) or True)

    with pytest.raises(ValueError, match="未知的文件夹类型"):
        local_files.open_local_folder("../private")

    assert opened == []


def test_local_folder_reports_desktop_unavailable(isolated_folders, monkeypatch):
    monkeypatch.setattr(local_files, "open_path", lambda _path: False)

    with pytest.raises(OSError, match="无法打开本机文件管理器"):
        local_files.open_local_folder("clips")
