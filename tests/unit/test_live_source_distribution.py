"""直播源接口、示例与文档的分发白名单一致性。"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

import pytest
from setuptools import Distribution

ROOT = Path(__file__).resolve().parents[2]
RESOURCES = {
    "docs/development/live-source-plugins.md",
    "docs/development/douyin-plugin-handoff.md",
    "plugin/README.md",
    "plugin/manifest.schema.json",
    "plugin/live-source-example/main.py",
    "plugin/live-source-example/plugin.json",
    "plugin/live-source-example/README.md",
}


def test_source_example_uses_only_public_host_imports() -> None:
    module = ast.parse((ROOT / "plugin/live-source-example/main.py").read_text(encoding="utf-8"))
    allowed = {"app.plugins", "app.plugins.live_source"}
    imports: set[str] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app."):
            imports.add(node.module)
        elif isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names if alias.name.startswith("app."))
    assert imports == allowed


def test_live_source_resources_are_declared_in_wheel_and_portable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packaging/portable/src"))
    from blc_portable.payload.builder import _should_include
    from blc_portable.payload.file_plan import PAYLOAD_ITEMS
    from blc_portable.payload.source_snapshot import PAYLOAD_ITEMS as BASELINE_ITEMS

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    data_files = project["tool"]["setuptools"]["data-files"]
    packaged = {path for files in data_files.values() for path in files}
    assert RESOURCES <= packaged
    assert PAYLOAD_ITEMS == BASELINE_ITEMS
    for path in RESOURCES:
        assert (ROOT / path).is_file(), path
        assert _should_include(path), path
        assert any(path == item or item.endswith("/") and path.startswith(item) for item in BASELINE_ITEMS), path
    for private in (".env", "storage/live.db", "docs/audit/private.md", "plugin/other-plugin/main.py"):
        assert not _should_include(private), private
    assert _should_include("app/plugins/live_source.py")


def test_installed_plugin_contract_links_resolve_to_original_documents(tmp_path: Path) -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    data_files = project["tool"]["setuptools"]["data-files"]
    distribution = Distribution(
        {
            "data_files": [
                (destination, [str(ROOT / name) for name in names]) for destination, names in data_files.items()
            ]
        }
    )
    installer = distribution.get_command_obj("install_data")
    installer.install_dir = str(tmp_path)
    installer.ensure_finalized()
    installer.run()

    originals = {Path(name).name: ROOT / name for name in RESOURCES if name.startswith("docs/")}
    contract_links = 0
    for readme in (tmp_path / "share/bili-live-cut/plugin").rglob("README.md"):
        for href in re.findall(r"\]\(([^)]+)\)", readme.read_text(encoding="utf-8")):
            relative = href.split("#", maxsplit=1)[0]
            original = originals.get(Path(relative).name)
            if original is None:
                continue
            installed = (readme.parent / relative).resolve()
            assert installed.is_relative_to(tmp_path.resolve())
            assert installed.is_file(), f"{readme.name}: {href}"
            assert installed.read_bytes() == original.read_bytes()
            contract_links += 1
    assert contract_links >= 3
