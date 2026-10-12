"""视频编码设置到实际 FFmpeg 命令的回归。"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from app.clipping import core
from app.clipping.models import ClipOptions

if TYPE_CHECKING:
    from pytest import MonkeyPatch


@pytest.mark.parametrize(
    "options,encoder,quality_flag,quality",
    [
        (ClipOptions(crf=19, preset="slow"), "libx264", "-crf", "19"),
        (ClipOptions(video_encoder="nvidia", hardware_quality=17), "h264_nvenc", "-qp", "17"),
        (ClipOptions(video_encoder="intel", hardware_quality=18), "h264_qsv", "-global_quality", "18"),
        (ClipOptions(video_encoder="amd", hardware_quality=16), "h264_amf", "-qp_p", "16"),
    ],
)
def test_clip_command_uses_selected_encoder_and_backend_quality(
    tmp_path: Path, monkeypatch: MonkeyPatch, options: ClipOptions, encoder: str, quality_flag: str, quality: str
) -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(core, "run_cancellable", run)
    core._run_ffmpeg_clip(tmp_path / "list.txt", tmp_path / "clip.mp4", 2, 3, options, None)
    command = calls[0]
    assert len(calls) == 1
    assert command[command.index("-c:v") + 1] == encoder
    assert command[command.index(quality_flag) + 1] == quality
    assert "trim=start=2" in command[command.index("-vf") + 1]
    assert ("-crf" in command) == (encoder == "libx264")
    assert command[-1] == str(tmp_path / "clip.mp4")


def test_missing_hardware_encoder_fails_without_cpu_retry(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, b"", b"Unknown encoder 'h264_nvenc'")

    monkeypatch.setattr(core, "run_cancellable", run)
    with pytest.raises(RuntimeError, match="encoder=nvidia"):
        core._run_ffmpeg_clip(
            tmp_path / "list.txt", tmp_path / "clip.mp4", 0, 3, ClipOptions(video_encoder="nvidia"), None
        )
    assert len(calls) == 1
    assert "libx264" not in calls[0]


@pytest.mark.parametrize("preset,expected", [("speed", "p1"), ("balanced", "p4"), ("quality", "p7")])
def test_nvidia_uses_its_own_preset_values(preset: str, expected: str) -> None:
    # 配置入口的严格验证单独覆盖；此处通过模型构造获得准确的 Literal 类型。
    from app.core.config import Settings

    config = Settings(_env_file=None, clip_hardware_preset=preset)
    options = ClipOptions(video_encoder="nvidia", hardware_preset=config.clip_hardware_preset)
    command = options.video_encoding_args()
    assert command[command.index("-preset") + 1] == expected
