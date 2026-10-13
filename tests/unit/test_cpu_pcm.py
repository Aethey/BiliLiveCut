"""PCM CPU 内核与真实 WAV 输入的数值、字节序和取消回归。"""

from __future__ import annotations

import math
import random
import struct
import wave
from collections.abc import Callable
from pathlib import Path

import pytest

from app.accelerators import dispatcher
from app.accelerators.python_fallback.speedups_round2 import pcm16_statistics as reference
from app.analysis.transcription.audio_normalization import inspect_pcm_audio

Kernel = Callable[[bytes], tuple[int, int, int]]


@pytest.mark.parametrize("kernel", [reference, dispatcher.pcm16_statistics])
@pytest.mark.parametrize("values", [[], [0], [-32768], [32767], [-32768, 32767, -1, 1, 0, 256], [123] * 65537])
def test_pcm_exact_integers(kernel: Kernel, values: list[int]) -> None:
    raw = struct.pack(f"<{len(values)}h", *values)
    assert kernel(raw) == (len(values), sum(v * v for v in values), max(map(abs, values), default=0))


@pytest.mark.parametrize("kernel", [reference, dispatcher.pcm16_statistics])
def test_pcm_covers_every_int16_value_and_internal_block_boundary(kernel: Kernel) -> None:
    values = list(range(-32768, 32768)) * 3 + [-32768]
    raw = struct.pack(f"<{len(values)}h", *values)
    assert kernel(raw) == (len(values), sum(v * v for v in values), 32768)
    assert kernel(bytes([0, 1, 1, 0, 255, 127, 0, 128])) == (4, 256**2 + 1 + 32767**2 + 32768**2, 32768)


@pytest.mark.parametrize("kernel", [reference, dispatcher.pcm16_statistics])
@pytest.mark.parametrize("raw", [b"x", b"abc", bytes(131073)], ids=["one-byte", "three-bytes", "odd-block"])
def test_pcm_rejects_partial_sample(kernel: Kernel, raw: bytes) -> None:
    with pytest.raises(ValueError, match="不完整采样"):
        kernel(raw)


def test_pcm_random_blocks_match_integer_oracle() -> None:
    rng = random.Random(1809)
    for size in [1, 2, 17, 63, 65535, 65536, 65537, 131073]:
        values = [rng.randrange(-32768, 32768) for _ in range(size)]
        raw = struct.pack(f"<{size}h", *values)
        assert dispatcher.pcm16_statistics(raw) == reference(raw)


def write_wav(path: Path, samples: list[int]) -> None:
    """生成固定小端 PCM16 测试音频。"""
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        stream.writeframes(struct.pack(f"<{len(samples)}h", *samples))


def test_wav_evidence_preserves_energy_across_blocks(tmp_path: Path) -> None:
    samples = [-32768, 32767, 0, -100, 200] * 28000
    path = tmp_path / "audio.wav"
    write_wav(path, samples)
    evidence = inspect_pcm_audio(str(path))
    assert evidence.sample_count == len(samples)
    assert evidence.duration_s == len(samples) / 16000
    assert evidence.rms == math.sqrt(sum(v * v for v in samples) / len(samples)) / 32768
    assert evidence.peak == 1.0


def test_wav_cancellation_between_blocks_still_aborts(tmp_path: Path) -> None:
    path = tmp_path / "audio.wav"
    write_wav(path, [1] * 131073)
    calls = 0

    def cancel() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("cancelled scan")

    with pytest.raises(RuntimeError, match="cancelled scan"):
        inspect_pcm_audio(str(path), check_cancelled=cancel)
    assert calls == 2


@pytest.mark.parametrize("remove", [1, 2, 4])
def test_wav_truncation_still_rejected(tmp_path: Path, remove: int) -> None:
    path = tmp_path / "audio.wav"
    write_wav(path, [-32768, 32767, 0])
    path.write_bytes(path.read_bytes()[:-remove])
    with pytest.raises(ValueError, match="不完整采样|帧数无效"):
        inspect_pcm_audio(str(path))


@pytest.mark.parametrize("kernel", [reference, dispatcher.pcm16_statistics])
@pytest.mark.parametrize("raw", [None, bytearray(b"ab"), memoryview(b"ab"), "ab", 2])
def test_pcm_rejects_nonimmutable_bytes(kernel: Kernel, raw: object) -> None:
    with pytest.raises(TypeError):
        kernel(raw)  # type: ignore[arg-type]
