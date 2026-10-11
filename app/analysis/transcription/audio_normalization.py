"""为 ASR 标准化媒体输入。"""

from __future__ import annotations

import math
import os
import subprocess
import sys
import time
import wave
from array import array
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from loguru import logger

from app.analysis.transcription.process_tree import ProcessTree
from app.core.config import settings

_ASR_SAMPLE_RATE = 16_000
_NORMALIZE_TIMEOUT_S = 300


@dataclass(frozen=True)
class AudioEvidence:
    """来自已完整读取 PCM 样本的事实；不使用探测失败的零值。"""

    sample_count: int
    sample_rate: int
    duration_s: float
    rms: float
    peak: float


def inspect_pcm_audio(path: str, *, check_cancelled: Callable[[], None] | None = None) -> AudioEvidence:
    """校验标准 WAV 的真实帧数并计算能量，拒绝空音轨、截断和格式错配。"""
    with wave.open(path, "rb") as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (
            1,
            2,
            _ASR_SAMPLE_RATE,
            "NONE",
        ):
            raise ValueError("ASR 需要 16kHz 单声道 PCM16 WAV")
        expected = audio.getnframes()
        count = 0
        square_sum = 0
        peak = 0
        while raw := audio.readframes(65536):
            if check_cancelled is not None:
                check_cancelled()
            if len(raw) % 2:
                raise ValueError("ASR WAV 含不完整采样")
            samples = array("h", raw)
            if sys.byteorder != "little":
                samples.byteswap()
            count += len(samples)
            square_sum += sum(value * value for value in samples)
            peak = max(peak, max(abs(value) for value in samples))
        if count == 0 or count != expected:
            raise ValueError(f"ASR WAV 帧数无效: declared={expected} decoded={count}")
        return AudioEvidence(
            count, _ASR_SAMPLE_RATE, count / _ASR_SAMPLE_RATE, math.sqrt(square_sum / count) / 32768, peak / 32768
        )


def _run_normalization(
    command: list[str], check_cancelled: Callable[[], None] | None
) -> subprocess.CompletedProcess[str]:
    """有任务控制器时轮询 FFmpeg 并在取消后等待退出，避免遗留解码进程。"""
    if check_cancelled is None:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_NORMALIZE_TIMEOUT_S,
            check=False,
        )
    check_cancelled()
    started = time.monotonic()
    with subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=os.name != "nt",
    ) as process:
        try:
            tree = ProcessTree(process.pid)
        except BaseException:
            process.kill()
            process.communicate(timeout=5)
            raise
        try:
            while True:
                check_cancelled()
                if time.monotonic() - started > _NORMALIZE_TIMEOUT_S:
                    raise subprocess.TimeoutExpired(command, _NORMALIZE_TIMEOUT_S)
                try:
                    stdout, stderr = process.communicate(timeout=0.2)
                    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
                except subprocess.TimeoutExpired:
                    continue
        finally:
            tree.terminate()
            if process.poll() is None:
                process.communicate(timeout=5)


@contextmanager
def normalized_asr_audio(source_path: str, *, check_cancelled: Callable[[], None] | None = None) -> Iterator[str]:
    """把非 WAV 媒体解码为临时的 16kHz 单声道 PCM WAV。

    录制器产出的是带视频流的 TS；直接把五分钟 TS 交给生成式 ASR 容易让
    解码器处理超长单句。WAV 输入通常来自本模块或局部复核截取，可直接复用。

    :param source_path: 原始媒体文件路径。
    :yields: 可供 ASR 使用的 WAV 路径或原始 WAV 路径。
    :raises RuntimeError: 文件不存在、FFmpeg 超时或转换失败时。
    """
    source = Path(source_path)
    if not source.is_file():
        raise RuntimeError(f"ASR 输入文件不存在: {source}")
    if source.suffix.casefold() in {".wav", ".wave"}:
        try:
            with wave.open(str(source), "rb") as audio:
                standard = (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) == (
                    1,
                    2,
                    _ASR_SAMPLE_RATE,
                    "NONE",
                )
        except (wave.Error, EOFError):
            standard = False  # 其他合法 WAV 编码交由 FFmpeg；损坏输入由解码错误报告。
        if standard:
            inspect_pcm_audio(str(source), check_cancelled=check_cancelled)
            yield str(source)
            return

    with TemporaryDirectory(prefix="blc-asr-") as temp_dir:
        output = Path(temp_dir) / "audio-16k-mono.wav"
        command = [
            settings.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(_ASR_SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            str(output),
        ]
        try:
            completed = _run_normalization(command, check_cancelled)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"ASR 音频标准化超时（>{_NORMALIZE_TIMEOUT_S}s）: {source}") from exc
        except OSError as exc:
            raise RuntimeError(f"无法启动 FFmpeg 标准化 ASR 音频: {exc}") from exc

        if completed.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
            detail = (completed.stderr or completed.stdout or "FFmpeg 未生成音频").strip()
            raise RuntimeError(f"ASR 音频标准化失败: {detail[:500]}")

        inspect_pcm_audio(str(output), check_cancelled=check_cancelled)
        logger.debug("ASR 输入已标准化: {} -> {}", source, output)
        yield str(output)


@contextmanager
def normalized_asr_audio_window(
    source_path: str,
    start_s: float,
    end_s: float,
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> Iterator[str]:
    """截取并标准化一个热点 ASR 局部窗口。

    窗口使用源媒体内的秒偏移；输出始终为 16kHz 单声道 PCM WAV，避免
    局部任务依赖系统编解码器或污染后台完整转写输入。

    :param source_path: 原始媒体文件路径。
    :param start_s: 窗口起始秒偏移（含）。
    :param end_s: 窗口结束秒偏移（不含）。
    :yields: 临时 WAV 路径。
    :raises ValueError: 窗口无效时。
    :raises RuntimeError: 输入、FFmpeg 或输出不可用时。
    """
    start = max(0.0, float(start_s))
    end = float(end_s)
    if end <= start:
        raise ValueError(f"热点 ASR 窗口无效: start={start:.3f}s end={end:.3f}s")

    source = Path(source_path)
    if not source.is_file():
        raise RuntimeError(f"ASR 输入文件不存在: {source}")

    with TemporaryDirectory(prefix="blc-asr-window-") as temp_dir:
        output = Path(temp_dir) / "hotspot-16k-mono.wav"
        command = [
            settings.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{end - start:.3f}",
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(_ASR_SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            str(output),
        ]
        try:
            completed = _run_normalization(command, check_cancelled)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"热点 ASR 音频截取超时（>{_NORMALIZE_TIMEOUT_S}s）: {source}") from exc
        except OSError as exc:
            raise RuntimeError(f"无法启动 FFmpeg 截取热点 ASR 音频: {exc}") from exc

        if completed.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
            detail = (completed.stderr or completed.stdout or "FFmpeg 未生成音频").strip()
            raise RuntimeError(f"热点 ASR 音频截取失败: {detail[:500]}")

        inspect_pcm_audio(str(output), check_cancelled=check_cancelled)
        logger.debug("热点 ASR 输入已截取: {} [{:.3f}, {:.3f}) -> {}", source, start, end, output)
        yield str(output)
