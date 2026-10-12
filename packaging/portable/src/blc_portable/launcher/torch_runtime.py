"""Explicit, hash-locked Windows Torch runtime selection and persistence."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

RUNTIMES = ("cpu", "cu126", "cu130")


@dataclass(frozen=True, slots=True)
class TorchWheel:
    """One immutable artifact from the official PyTorch Windows index."""

    package: str
    version: str
    filename: str
    url: str
    sha256: str


def runtime_config_path(app_root: Path) -> Path:
    """Keep the dependency choice separate from business and Web port settings."""
    return app_root / "config" / "torch-runtime.json"


def load_torch_runtime(app_root: Path) -> str | None:
    """Read the persisted choice; corrupt configuration must never imply CPU."""
    path = runtime_config_path(app_root)
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(raw, dict)
        or set(raw) != {"schema", "runtime"}
        or type(raw["schema"]) is not int
        or raw["schema"] != 1
        or raw["runtime"] not in RUNTIMES
    ):
        raise ValueError("config/torch-runtime.json 无效；拒绝自动替换已安装的 Torch 运行时")
    return str(raw["runtime"])


def save_torch_runtime(app_root: Path, runtime: str) -> None:
    """Atomically retain a choice only after installation and preflight succeed."""
    if runtime not in RUNTIMES:
        raise ValueError("不支持的 Torch 运行时")
    path = runtime_config_path(app_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump({"schema": 1, "runtime": runtime}, handle)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def cuda_wheels(base_lock: Path, runtime: str) -> tuple[TorchWheel, ...]:
    """Validate official URLs, ABI, hashes and compatibility with the base lock."""
    if runtime not in RUNTIMES[1:]:
        raise ValueError("CUDA 运行时必须是 cu126 或 cu130")
    match = re.fullmatch(r"requirements-runtime-(py31[12])-win-x64\.lock", base_lock.name)
    if match is None:
        raise ValueError("不支持的 Windows Python ABI")
    abi = match[1]
    lock = base_lock.with_name(f"requirements-torch-{runtime}-{abi}-win-x64.lock")
    base_versions = dict(
        re.findall(r"^(torch|torchaudio)==([^\s]+)", base_lock.read_text(encoding="utf-8"), re.MULTILINE)
    )
    wheels: list[TorchWheel] = []
    for line in lock.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        entry = re.fullmatch(r"(torch|torchaudio) @ (https://\S+) --hash=sha256:([a-f0-9]{64})", line)
        if entry is None:
            raise ValueError(f"CUDA lock entry invalid: {lock.name}")
        package, url, digest = entry.groups()
        parsed = urlsplit(url)
        version = f"{base_versions[package]}+{runtime}"
        filename = f"{package}-{version}-{abi.replace('py', 'cp')}-{abi.replace('py', 'cp')}-win_amd64.whl"
        if (
            parsed.netloc != "download-r2.pytorch.org"
            or unquote(parsed.path) != f"/whl/{runtime}/{filename}"
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(f"CUDA wheel does not match the official locked artifact: {package}")
        wheels.append(TorchWheel(package, version, filename, url, digest))
    if sorted(wheel.package for wheel in wheels) != ["torch", "torchaudio"]:
        raise ValueError("CUDA lock must contain exactly one Torch and TorchAudio wheel")
    return tuple(wheels)


def choose_torch_runtime(
    base_lock: Path, installed: Mapping[str, str], *, saved: str | None, requested: str | None
) -> str:
    """Preserve a recognized manual CUDA installation instead of reverting it."""
    selected = requested if requested is not None else saved
    if selected is not None:
        if selected not in RUNTIMES:
            raise ValueError("不支持的 Torch 运行时")
        return selected
    if any("+cu" in installed.get(package, "") for package in ("torch", "torchaudio")):
        for runtime in RUNTIMES[1:]:
            if all(installed.get(wheel.package) == wheel.version for wheel in cuda_wheels(base_lock, runtime)):
                return runtime
        raise RuntimeError(
            "已有 CUDA Torch/TorchAudio 与锁不一致；为避免覆盖，已停止。"
            "请显式使用 --torch-runtime cu126、cu130 或 cpu 修复配套依赖。"
        )
    return "cpu"


def verify_torch_runtime(python: Path, base_lock: Path, runtime: str, environment: Mapping[str, str]) -> None:
    """Import both installed packages and verify their actual build before persisting."""
    expected = (
        {wheel.package: wheel.version for wheel in cuda_wheels(base_lock, runtime)}
        if runtime != "cpu"
        else dict(re.findall(r"^(torch|torchaudio)==([^\s]+)", base_lock.read_text(encoding="utf-8"), re.MULTILINE))
    )
    expected_cuda = {"cpu": None, "cu126": "12.6", "cu130": "13.0"}[runtime]
    script = (
        "import importlib.metadata as m, json, sys; import torch, torchaudio; "
        "expected=json.loads(sys.argv[1]); actual={p:m.version(p) for p in expected}; "
        "assert actual==expected, f'Torch package versions mismatch: {actual}'; "
        "cuda=json.loads(sys.argv[2]); "
        "assert torch.version.cuda==cuda, f'Torch CUDA build mismatch: {torch.version.cuda}'; "
        "print(json.dumps({'torch':str(torch.__version__),'torchaudio':str(torchaudio.__version__),"
        "'cuda_build':torch.version.cuda,'cuda_available':torch.cuda.is_available()}))"
    )
    result = subprocess.run(
        [str(python), "-c", script, json.dumps(expected), json.dumps(expected_cuda)],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
        env=dict(environment),
    )
    print(f"  Torch actual runtime: {result.stdout.strip()}")
    subprocess.run(
        [str(python), "-m", "pip", "check"],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
        env=dict(environment),
    )


@contextmanager
def install_lock(base_lock: Path, runtime: str, app_root: Path, *, offline: bool) -> Iterator[Path]:
    """Overlay only Torch wheels; retain all other locked packages and hashes."""
    if runtime == "cpu":
        yield base_lock
        return
    requirements = [
        line
        for line in base_lock.read_text(encoding="utf-8").splitlines()
        if not re.match(r"^(torch|torchaudio)==", line)
    ]
    for wheel in cuda_wheels(base_lock, runtime):
        local = app_root / "vendor" / "wheels" / wheel.filename
        if local.is_file():
            reference = local.resolve().as_uri()
        elif offline:
            raise RuntimeError(f"离线 CUDA 安装缺少 {local}；请预先放入官方锁定 wheel，不能联网回退")
        else:
            reference = wheel.url
        requirements.append(f"{wheel.package} @ {reference} --hash=sha256:{wheel.sha256}")
    with tempfile.TemporaryDirectory(prefix="blc-torch-lock-") as temporary:
        path = Path(temporary) / "requirements.lock"
        path.write_text("\n".join(requirements) + "\n", encoding="utf-8")
        yield path
