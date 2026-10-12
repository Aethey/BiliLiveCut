"""Exercise the release fixture builder through the real Engine Pack installer."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from blc_portable.engine_pack import builder
from blc_portable.engine_pack.installer import (
    EnginePackValidationError,
    check_installed_models,
    install_from_engine_pack,
)
from blc_portable.engine_pack.manifest import SOURCE_COMMIT_FULL, create_manifest


def _pack_fixture(staging: Path, archive: Path) -> None:
    manifest = create_manifest(
        source_commit=SOURCE_COMMIT_FULL,
        builder_commit=SOURCE_COMMIT_FULL,
        file_list=builder.build_file_list(staging),
        fixture=True,
    )
    (staging / "engine-pack-manifest.json").write_text(
        json.dumps(manifest.to_dict(), ensure_ascii=False), encoding="utf-8"
    )
    builder.create_zip(staging, archive)


def test_release_builder_fixture_installs_all_catalog_engines(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    builder.build_fixture(staging)
    for engine in builder.load_engines():
        for relative in engine.required_files:
            assert (staging / engine.target_path / relative).stat().st_size > 0
    archive = tmp_path / "fixture.zip"
    _pack_fixture(staging, archive)
    app_root = tmp_path / "app"
    result = install_from_engine_pack(app_root, archive, "", "")
    assert set(result["installed_engines"]) == {engine.engine_id for engine in builder.load_engines()}
    assert check_installed_models(app_root / "models", full_rehash=True) == (True, [])


@pytest.mark.parametrize("damage", ["missing", "empty"])
def test_fixture_required_weight_is_validated_even_with_matching_manifest(tmp_path: Path, damage: str) -> None:
    staging = tmp_path / "staging"
    builder.build_fixture(staging)
    engine = builder.load_engines()[0]
    weight = staging / engine.target_path / engine.required_files[0]
    if damage == "missing":
        weight.unlink()
    else:
        weight.write_bytes(b"")
    archive = tmp_path / "damaged.zip"
    _pack_fixture(staging, archive)
    app_root = tmp_path / "app"
    with pytest.raises(EnginePackValidationError, match="required file missing or empty"):
        install_from_engine_pack(app_root, archive, "", "")
    assert not (app_root / "models").exists()


def test_fixture_builder_rejects_required_path_outside_model_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = replace(builder.load_engines()[0], required_files=["../../outside.bin"])
    monkeypatch.setattr(builder, "load_engines", lambda: [engine])
    with pytest.raises(ValueError, match="escapes model directory"):
        builder.build_fixture(tmp_path)
    assert not (tmp_path / "outside.bin").exists()
