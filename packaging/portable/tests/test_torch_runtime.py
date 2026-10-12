"""GPU runtime installation retains offline and immutable-dependency guarantees."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_portable_src = Path(__file__).resolve().parents[1] / "src"
if str(_portable_src) not in sys.path:
    sys.path.insert(0, str(_portable_src))

from blc_portable.launcher import torch_runtime as runtime

LOCKS = Path(__file__).resolve().parents[1] / "locks"


@pytest.fixture
def base_lock(tmp_path: Path) -> Path:
    for path in LOCKS.glob("requirements-torch-*-py312-win-x64.lock"):
        shutil.copy2(path, tmp_path / path.name)
    base = tmp_path / "requirements-runtime-py312-win-x64.lock"
    base.write_text(
        "torch==2.13.0 --hash=sha256:" + "a" * 64 + "\n"
        "torchaudio==2.11.0 --hash=sha256:" + "b" * 64 + "\n"
        "example==1.0 --hash=sha256:" + "c" * 64 + "\n"
    )
    return base


@pytest.mark.parametrize("abi", ["311", "312"])
@pytest.mark.parametrize("choice", ["cu126", "cu130"])
def test_all_official_cuda_locks_match_base_versions_and_abi(abi: str, choice: str) -> None:
    wheels = runtime.cuda_wheels(LOCKS / f"requirements-runtime-py{abi}-win-x64.lock", choice)
    assert {wheel.package for wheel in wheels} == {"torch", "torchaudio"}
    assert all(f"cp{abi}-cp{abi}-win_amd64.whl" in wheel.filename for wheel in wheels)
    assert all(len(wheel.sha256) == 64 for wheel in wheels)


def test_recognized_manual_cuda_pair_is_preserved(base_lock: Path) -> None:
    selected = runtime.choose_torch_runtime(
        base_lock, {"torch": "2.13.0+cu126", "torchaudio": "2.11.0+cu126"}, saved=None, requested=None
    )
    assert selected == "cu126"


@pytest.mark.parametrize(
    "installed",
    [
        {"torch": "2.13.0+cu126", "torchaudio": "2.11.0"},
        {"torch": "2.13.0+cu130", "torchaudio": "2.11.0+cu126"},
        {"torch": "2.12.0+cu126", "torchaudio": "2.11.0+cu126"},
    ],
)
def test_unknown_or_mixed_cuda_pair_is_not_silently_overwritten(base_lock: Path, installed: dict[str, str]) -> None:
    with pytest.raises(RuntimeError, match="为避免覆盖"):
        runtime.choose_torch_runtime(base_lock, installed, saved=None, requested=None)


def test_explicit_cpu_selection_can_replace_cuda(base_lock: Path) -> None:
    assert runtime.choose_torch_runtime(base_lock, {"torch": "2.13.0+cu130"}, saved="cu126", requested="cpu") == "cpu"


def test_persisted_choice_survives_restart(tmp_path: Path) -> None:
    assert runtime.load_torch_runtime(tmp_path) is None
    runtime.save_torch_runtime(tmp_path, "cu126")
    assert runtime.load_torch_runtime(tmp_path) == "cu126"


@pytest.mark.parametrize(
    "payload",
    [
        {"schema": True, "runtime": "cpu"},
        {"schema": 1, "runtime": "other"},
        {"schema": 1, "runtime": "cu126", "extra": True},
        [],
    ],
)
def test_corrupt_choice_never_defaults_to_cpu(tmp_path: Path, payload: object) -> None:
    path = runtime.runtime_config_path(tmp_path)
    path.parent.mkdir()
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        runtime.load_torch_runtime(tmp_path)


def test_failed_atomic_save_retains_previous_choice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime.save_torch_runtime(tmp_path, "cpu")

    def fail_replace(source: Path, destination: Path) -> None:
        raise PermissionError("locked configuration")

    monkeypatch.setattr(runtime.os, "replace", fail_replace)
    with pytest.raises(PermissionError):
        runtime.save_torch_runtime(tmp_path, "cu126")
    assert runtime.load_torch_runtime(tmp_path) == "cpu"
    assert list((tmp_path / "config").iterdir()) == [runtime.runtime_config_path(tmp_path)]


def test_gpu_overlay_preserves_other_locked_dependencies(base_lock: Path, tmp_path: Path) -> None:
    with runtime.install_lock(base_lock, "cu126", tmp_path, offline=False) as lock:
        text = lock.read_text()
        assert "example==1.0 --hash=sha256:" + "c" * 64 in text
        assert "torch==2.13.0" not in text and "torchaudio==2.11.0" not in text
        assert text.count(" @ https://download-r2.pytorch.org/whl/cu126/") == 2
        assert text.count("--hash=sha256:") == 3
    assert not lock.exists()


def test_offline_gpu_install_requires_local_wheels(base_lock: Path, tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="离线 CUDA 安装缺少"):
        with runtime.install_lock(base_lock, "cu126", tmp_path, offline=True):
            pytest.fail("missing offline wheels must not yield an installable lock")


def test_offline_gpu_overlay_contains_only_local_urls_with_original_hashes(base_lock: Path, tmp_path: Path) -> None:
    wheelhouse = tmp_path / "vendor/wheels"
    wheelhouse.mkdir(parents=True)
    wheels = runtime.cuda_wheels(base_lock, "cu126")
    for wheel in wheels:
        (wheelhouse / wheel.filename).write_bytes(b"tampered wheel")
    with runtime.install_lock(base_lock, "cu126", tmp_path, offline=True) as lock:
        text = lock.read_text()
        assert "https://" not in text and text.count(" @ file://") == 2
        assert all(f"--hash=sha256:{wheel.sha256}" in text for wheel in wheels)


@pytest.mark.parametrize(
    "mutation",
    [
        ("download-r2.pytorch.org", "evil.invalid"),
        ("%2Bcu126-cp312", "%2Bcu130-cp312"),
        ("-cp312-cp312-win_amd64", "-cp311-cp311-win_amd64"),
        (" --hash=sha256:", " --trusted-host evil.invalid --hash=sha256:"),
    ],
)
def test_tampered_cuda_catalog_is_rejected(base_lock: Path, mutation: tuple[str, str]) -> None:
    lock = base_lock.with_name("requirements-torch-cu126-py312-win-x64.lock")
    lock.write_text(lock.read_text().replace(*mutation))
    with pytest.raises(ValueError):
        runtime.cuda_wheels(base_lock, "cu126")


@pytest.mark.parametrize("saved", [None, "cu126"])
def test_selected_gpu_restart_does_not_reinstall_cpu(
    base_lock: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, saved: str | None
) -> None:
    from blc_portable.launcher import main

    if saved is not None:
        runtime.save_torch_runtime(tmp_path, saved)
    monkeypatch.setattr(main, "_find_lock_file", lambda python: base_lock)
    commands: list[list[str]] = []

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        output = (
            json.dumps(
                [
                    {"name": "torch", "version": "2.13.0+cu126"},
                    {"name": "torchaudio", "version": "2.11.0+cu126"},
                    {"name": "example", "version": "1.0"},
                ]
            )
            if "list" in args
            else "{}"
        )
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

    monkeypatch.setattr(main.subprocess, "run", run)
    main.install_dependencies(Path("python.exe"), tmp_path)
    assert not any("install" in args for args in commands)
    assert any("check" in args for args in commands)
    assert runtime.load_torch_runtime(tmp_path) == "cu126"


@pytest.mark.parametrize("inventory", ["invalid JSON", "{}", '[{"name":"torch","version":null}]'])
def test_invalid_inventory_cannot_trigger_cpu_replacement(
    base_lock: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inventory: str
) -> None:
    from blc_portable.launcher import main

    monkeypatch.setattr(main, "_find_lock_file", lambda python: base_lock)
    commands: list[list[str]] = []

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=inventory, stderr="")

    monkeypatch.setattr(main.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        main.install_dependencies(Path("python.exe"), tmp_path)
    assert not any("install" in args for args in commands)
    assert runtime.load_torch_runtime(tmp_path) is None


def test_gpu_preflight_failure_does_not_commit_new_choice(
    base_lock: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from blc_portable.launcher import main

    runtime.save_torch_runtime(tmp_path, "cpu")
    monkeypatch.setattr(main, "_find_lock_file", lambda python: base_lock)

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "-c" in args and "import torch, torchaudio" in args[args.index("-c") + 1]:
            raise subprocess.CalledProcessError(1, args, stderr="CUDA build mismatch")
        output = (
            json.dumps(
                [
                    {"name": "torch", "version": "2.13.0+cu126"},
                    {"name": "torchaudio", "version": "2.11.0+cu126"},
                    {"name": "example", "version": "1.0"},
                ]
            )
            if "list" in args
            else "{}"
        )
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

    monkeypatch.setattr(main.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        main.install_dependencies(Path("python.exe"), tmp_path, torch_runtime="cu126")
    assert runtime.load_torch_runtime(tmp_path) == "cpu"


def test_offline_missing_gpu_wheels_stop_before_pip_install(
    base_lock: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from blc_portable.launcher import main

    wheelhouse = tmp_path / "vendor/wheels"
    wheelhouse.mkdir(parents=True)
    (wheelhouse / "cpu-placeholder.whl").write_bytes(b"fixture")
    monkeypatch.setenv("BLC_OFFLINE", "1")
    monkeypatch.setattr(main, "_find_lock_file", lambda python: base_lock)
    commands: list[list[str]] = []

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=json.dumps(
                [
                    {"name": "torch", "version": "2.13.0"},
                    {"name": "torchaudio", "version": "2.11.0"},
                    {"name": "example", "version": "1.0"},
                ]
            ),
            stderr="",
        )

    monkeypatch.setattr(main.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="离线 CUDA 安装缺少"):
        main.install_dependencies(Path("python.exe"), tmp_path, torch_runtime="cu126")
    assert not any("install" in args for args in commands)
    assert runtime.load_torch_runtime(tmp_path) is None
