"""The optional real-model check fails clearly before any implicit model setup."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_local_asr_command_requires_existing_audio_and_installed_models(tmp_path: Path) -> None:
    command = [sys.executable, "scripts/check_local_asr.py", "--audio", str(tmp_path / "missing.wav")]
    result = subprocess.run(
        [*command, "--models-dir", str(tmp_path)], capture_output=True, text=True, timeout=10, check=False
    )
    assert result.returncode == 2 and "existing nonempty local file" in result.stderr
    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"not yet decoded")
    result = subprocess.run(
        [sys.executable, "scripts/check_local_asr.py", "--audio", str(audio), "--models-dir", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 2 and "prepare the Engine Pack first" in result.stderr
