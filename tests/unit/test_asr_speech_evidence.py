"""空语音必须有真实音频证据；SDK/VAD 仅在外部模型边界使用替身。"""

from __future__ import annotations

import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from app.analysis.transcription.audio_normalization import inspect_pcm_audio, normalized_asr_audio
from app.analysis.transcription.backends import FasterWhisperBackend
from app.analysis.transcription.models import ASRTranscriptResult, EmotionEvent
from app.analysis.transcription.pipeline import ASRPipeline
from app.analysis.transcription.speech_evidence import no_speech_result, require_final_result
from app.core.config import settings


@pytest.mark.parametrize("signal", ["silence", "music", "noise"])
def test_valid_non_speech_bypasses_whisper_and_preserves_evidence(
    pcm_wav: Path, monkeypatch: pytest.MonkeyPatch, signal: str
) -> None:
    count = 133376
    samples = np.zeros(count, dtype="<i2")
    if signal == "music":
        samples = (np.sin(np.arange(count) * 2 * np.pi * 440 / 16000) * 6000).astype("<i2")
    elif signal == "noise":
        samples = np.random.default_rng(42).integers(-2000, 2000, count, dtype="<i2")
    with wave.open(str(pcm_wav), "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(samples.tobytes())
    monkeypatch.setattr("faster_whisper.vad.get_speech_timestamps", lambda audio: [])

    def forbidden(_self: FasterWhisperBackend) -> object:
        pytest.fail("VAD 明确无语音时不能进入 Whisper 原生推理")

    monkeypatch.setattr(FasterWhisperBackend, "_load_model", forbidden)
    result = FasterWhisperBackend().transcribe(str(pcm_wav))
    assert result.result_type == "no_speech"
    assert result.audio_duration == 8.336
    assert result.metadata["audio_evidence"]["sample_count"] == count
    assert (result.metadata["audio_evidence"]["rms"] == 0) == (signal == "silence")
    require_final_result(result)


def test_valid_speech_with_empty_generator_remains_failed(pcm_wav: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "asr_primary", "whisper")
    monkeypatch.setattr("faster_whisper.vad.get_speech_timestamps", lambda audio: [{"start": 0, "end": len(audio)}])
    model = SimpleNamespace(transcribe=lambda *args, **kwargs: (iter(()), SimpleNamespace(language="zh")))
    monkeypatch.setattr(FasterWhisperBackend, "_load_model", lambda self: model)
    with pytest.raises(RuntimeError, match="ASREmptyOutput"):
        ASRPipeline().transcribe(str(pcm_wav))


def test_all_engines_empty_preserves_music_and_laughter(pcm_wav: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "asr_primary", "funasr_nano")
    monkeypatch.setattr("faster_whisper.vad.get_speech_timestamps", lambda audio: [])
    events = [EmotionEvent("music", 0, 8.336), EmotionEvent("laughter", 1, 2)]
    backend = SimpleNamespace(
        transcribe_funasr=lambda *args: ASRTranscriptResult(text="", backend="funasr-nano", emotions=events[:1]),
        transcribe=lambda *args: ASRTranscriptResult(text="", backend="paraformer", emotions=events[1:]),
    )
    result = ASRPipeline(primary_backend=backend).transcribe(str(pcm_wav))
    assert result.result_type == "no_speech"
    assert {event.event_type for event in result.emotions} == {"music", "laughter"}
    assert result.metadata["audio_evidence"]["duration_s"] == 8.336


def test_model_exception_cannot_be_hidden_as_silence(pcm_wav: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "asr_primary", "paraformer")
    monkeypatch.setattr("faster_whisper.vad.get_speech_timestamps", lambda audio: [])

    def broken(*args: object) -> ASRTranscriptResult:
        raise ValueError("invalid model weights")

    backend = SimpleNamespace(transcribe=broken, primary_revision="v2")
    with pytest.raises(RuntimeError, match="invalid model weights") as caught:
        ASRPipeline(primary_backend=backend).transcribe(str(pcm_wav))
    assert isinstance(caught.value.__cause__, ValueError)


@pytest.mark.parametrize("corruption", ["zero_file", "zero_frames", "truncated", "missing"])
def test_invalid_input_is_not_no_speech(pcm_wav: Path, corruption: str) -> None:
    if corruption == "zero_file":
        pcm_wav.write_bytes(b"")
    elif corruption == "zero_frames":
        with wave.open(str(pcm_wav), "wb") as audio:
            audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            audio.writeframes(b"")
    elif corruption == "truncated":
        pcm_wav.write_bytes(pcm_wav.read_bytes()[:-100])
    else:
        pcm_wav.unlink()
    with pytest.raises((ValueError, EOFError, wave.Error, FileNotFoundError)):
        no_speech_result(str(pcm_wav), backend="whisper")


def test_vad_decoder_mismatch_is_failure(pcm_wav: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("faster_whisper.audio.decode_audio", lambda *args, **kwargs: np.zeros(3))
    with pytest.raises(ValueError, match="不一致"):
        no_speech_result(str(pcm_wav), backend="whisper")


def test_pcm_scan_observes_cancellation(pcm_wav: Path) -> None:
    def cancelled() -> None:
        raise RuntimeError("cancelled scan")

    with pytest.raises(RuntimeError, match="cancelled scan"):
        inspect_pcm_audio(str(pcm_wav), check_cancelled=cancelled)
    with pytest.raises(RuntimeError, match="cancelled scan"):
        with normalized_asr_audio(str(pcm_wav), check_cancelled=cancelled):
            pytest.fail("不能返回已取消音频")
