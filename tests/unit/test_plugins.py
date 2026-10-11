from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.plugins.manager import PluginManager, PluginStateError, PluginValidationError


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_enable", [False, True])
async def test_lazy_plugin_modules_unload_and_equal_length_update_is_visible(
    temp_db: None, tmp_path: Path, fail_enable: bool
) -> None:
    import os
    import sys

    directory = _write_plugin(tmp_path)
    package = directory / "v185_plugin_package"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    service = package / "service.py"
    service.write_text("VALUE = 'old'\n", encoding="utf-8")
    import py_compile

    py_compile.compile(str(service), doraise=True)
    original_mtime = service.stat().st_mtime
    entry = """from app.plugins import BasePlugin
import v185_plugin_package
class Plugin(BasePlugin):
    def on_enable(self, context):
        from v185_plugin_package import service
        (context.plugin_dir / 'value.txt').write_text(service.VALUE)
FAILURE
"""
    (directory / "main.py").write_text(
        entry.replace("FAILURE", "        raise RuntimeError('test')" if fail_enable else ""), encoding="utf-8"
    )
    manager = PluginManager(tmp_path)
    await manager.start()
    try:
        if fail_enable:
            with pytest.raises(PluginStateError):
                await manager.set_enabled("demo", True)
        else:
            await manager.set_enabled("demo", True)
            await manager.set_enabled("demo", False)
        assert not any(name.startswith("v185_plugin_package") for name in sys.modules)
        assert "app.plugins" in sys.modules
        service.write_text("VALUE = 'new'\n", encoding="utf-8")
        os.utime(service, (original_mtime, original_mtime))
        (directory / "main.py").write_text(entry.replace("FAILURE", ""), encoding="utf-8")
        await manager.set_enabled("demo", True)
        assert (directory / "value.txt").read_text() == "new"
    finally:
        await manager.stop()


def test_unloading_plugin_does_not_execute_unrelated_lazy_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys
    from types import ModuleType

    lazy = ModuleType("unrelated_lazy_dependency")
    touched: list[str] = []

    def lazy_import(name: str) -> object:
        touched.append(name)
        raise ModuleNotFoundError("optional vision dependency must not be imported")

    lazy.__getattr__ = lazy_import
    monkeypatch.setitem(sys.modules, lazy.__name__, lazy)
    plugin = ModuleType("local_plugin")
    plugin.__file__ = str(tmp_path / "main.py")
    monkeypatch.setitem(sys.modules, plugin.__name__, plugin)

    PluginManager._unload_modules(tmp_path)

    assert not touched
    assert sys.modules[lazy.__name__] is lazy
    assert plugin.__name__ not in sys.modules


def test_unloading_plugin_preserves_shared_namespace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib
    import sys

    roots = [tmp_path / "one", tmp_path / "two"]
    for index, root in enumerate(roots):
        package = root / "v185_shared_namespace"
        package.mkdir(parents=True)
        (package / f"child{index}.py").write_text(f"VALUE = {index}\n", encoding="utf-8")
        monkeypatch.syspath_prepend(str(root))
    first = importlib.import_module("v185_shared_namespace.child0")
    second = importlib.import_module("v185_shared_namespace.child1")
    parent = sys.modules["v185_shared_namespace"]
    try:
        PluginManager._unload_modules(roots[0])
        assert "v185_shared_namespace.child0" not in sys.modules
        assert not hasattr(parent, "child0")
        assert sys.modules["v185_shared_namespace"] is parent and parent.child1 is second
        assert first.VALUE == 0 and importlib.import_module("v185_shared_namespace.child1") is second
    finally:
        for name in tuple(sys.modules):
            if name.startswith("v185_shared_namespace"):
                sys.modules.pop(name, None)


def _write_plugin(root: Path, plugin_id: str = "demo") -> Path:
    directory = root / plugin_id
    directory.mkdir(parents=True)
    manifest = {
        "id": plugin_id,
        "name": "测试插件",
        "version": "1.0.0",
        "api_version": "1",
        "entrypoint": "main.py:Plugin",
        "description": "用于验证宿主插件契约",
        "settings_page": True,
    }
    (directory / "plugin.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    (directory / "main.py").write_text(
        """from app.plugins import BasePlugin, PluginSetting

class Plugin(BasePlugin):
    settings_schema = (
        PluginSetting(key="mode", label="模式", kind="select", choices=("safe", "fast"), default="safe"),
        PluginSetting(key="limit", label="上限", kind="number", default=2, minimum=1, maximum=5),
        PluginSetting(key="token", label="令牌", kind="password", default=""),
    )

    def on_enable(self, context):
        self.context = context
        (context.plugin_dir / "enabled.txt").write_text("enabled", encoding="utf-8")

    def on_disable(self):
        (self.context.plugin_dir / "disabled.txt").write_text("disabled", encoding="utf-8")
""",
        encoding="utf-8",
    )
    return directory


@pytest.mark.asyncio
async def test_scan_does_not_execute_until_explicit_enable(temp_db: None, tmp_path: Path) -> None:
    plugin_dir = _write_plugin(tmp_path)
    manager = PluginManager(tmp_path)

    await manager.start()
    payload = manager.list_payload()
    assert payload["scan_errors"] == []
    assert payload["plugins"][0]["enabled"] is False
    assert payload["plugins"][0]["loaded"] is False
    assert not (plugin_dir / "enabled.txt").exists()

    enabled = await manager.set_enabled("demo", True)
    assert enabled["enabled"] is True
    assert enabled["loaded"] is True
    assert (plugin_dir / "enabled.txt").read_text(encoding="utf-8") == "enabled"

    await manager.set_enabled("demo", False)
    assert (plugin_dir / "disabled.txt").read_text(encoding="utf-8") == "disabled"
    await manager.stop()


@pytest.mark.asyncio
async def test_plugin_settings_are_validated_namespaced_and_secrets_are_hidden(
    temp_db: None,
    tmp_path: Path,
) -> None:
    _write_plugin(tmp_path)
    manager = PluginManager(tmp_path)
    await manager.start()
    await manager.set_enabled("demo", True)

    initial = manager.settings_payload("demo")
    assert [field["key"] for field in initial["fields"]] == ["mode", "limit", "token"]
    updated = manager.update_settings("demo", {"mode": "fast", "limit": 4, "token": "secret"})
    values = {field["key"]: field for field in updated["fields"]}
    assert values["mode"]["value"] == "fast"
    assert values["limit"]["value"] == 4.0
    assert values["token"]["value"] == ""
    assert values["token"]["configured"] is True

    manager.update_settings("demo", {"token": ""})
    assert manager._read_setting("demo", "token", None) == "secret"  # noqa: SLF001
    with pytest.raises(PluginValidationError, match="允许选项"):
        manager.update_settings("demo", {"mode": "unsafe"})
    with pytest.raises(PluginValidationError, match="不能大于"):
        manager.update_settings("demo", {"limit": 10})
    with pytest.raises(PluginValidationError, match="未知设置项"):
        manager.update_settings("demo", {"other": True})
    with pytest.raises(PluginValidationError):
        manager.update_settings("demo", {"mode": "safe", "limit": 10})
    preserved = {field["key"]: field for field in manager.settings_payload("demo")["fields"]}
    assert preserved["mode"]["value"] == "fast"
    with pytest.raises(PluginValidationError, match="有限"):
        manager.update_settings("demo", {"limit": float("nan")})
    cleared = manager.update_settings("demo", {"token": None})
    assert next(field for field in cleared["fields"] if field["key"] == "token")["configured"] is False
    await manager.stop()


@pytest.mark.asyncio
async def test_invalid_manifest_is_reported_without_import(temp_db: None, tmp_path: Path) -> None:
    directory = tmp_path / "bad-name"
    directory.mkdir()
    (directory / "plugin.json").write_text(
        json.dumps(
            {
                "id": "different-name",
                "name": "坏清单",
                "version": "1",
                "api_version": "1",
                "entrypoint": "../outside.py:Plugin",
            }
        ),
        encoding="utf-8",
    )
    (directory / "main.py").write_text("raise RuntimeError('不得执行')", encoding="utf-8")
    manager = PluginManager(tmp_path)

    await manager.start()
    payload = manager.list_payload()
    assert payload["plugins"] == []
    assert payload["scan_errors"][0]["directory"] == "bad-name"
    assert "entrypoint" in payload["scan_errors"][0]["error"]
    await manager.stop()


@pytest.mark.asyncio
async def test_manifest_without_current_api_version_is_rejected(temp_db: None, tmp_path: Path) -> None:
    """插件清单不得为缺失的 api_version 补当前默认值。"""
    directory = _write_plugin(tmp_path)
    manifest_path = directory / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("api_version")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    manager = PluginManager(tmp_path)

    await manager.start()
    payload = manager.list_payload()

    assert payload["plugins"] == []
    assert "api_version" in payload["scan_errors"][0]["error"]
    await manager.stop()


@pytest.mark.asyncio
async def test_enabled_state_is_restored_on_next_manager_start(temp_db: None, tmp_path: Path) -> None:
    _write_plugin(tmp_path)
    first = PluginManager(tmp_path)
    await first.start()
    await first.set_enabled("demo", True)
    await first.stop()

    second = PluginManager(tmp_path)
    await second.start()
    descriptor = second.descriptor("demo")
    assert descriptor["enabled"] is True
    assert descriptor["loaded"] is True
    await second.stop()


@pytest.mark.asyncio
async def test_failed_enable_runs_cleanup_and_keeps_plugin_disabled(temp_db: None, tmp_path: Path) -> None:
    directory = tmp_path / "broken"
    directory.mkdir()
    (directory / "plugin.json").write_text(
        json.dumps(
            {
                "id": "broken",
                "name": "故障插件",
                "version": "1",
                "api_version": "1",
                "entrypoint": "main.py:Plugin",
            }
        ),
        encoding="utf-8",
    )
    (directory / "main.py").write_text(
        """from app.plugins import BasePlugin

class Plugin(BasePlugin):
    def on_enable(self, context):
        self.directory = context.plugin_dir
        raise RuntimeError("初始化失败")

    def on_disable(self):
        (self.directory / "rolled-back.txt").write_text("ok", encoding="utf-8")
""",
        encoding="utf-8",
    )
    manager = PluginManager(tmp_path)
    await manager.start()

    with pytest.raises(PluginStateError, match="初始化失败"):
        await manager.set_enabled("broken", True)
    descriptor = manager.descriptor("broken")
    assert descriptor["enabled"] is False
    assert descriptor["loaded"] is False
    assert (directory / "rolled-back.txt").read_text(encoding="utf-8") == "ok"
    await manager.stop()
