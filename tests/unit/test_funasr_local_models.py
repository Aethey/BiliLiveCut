from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from app.analysis.model_pool import model_pool
from app.analysis.transcription.backends import FunASRBackend
from app.analysis.transcription.pipeline import ASRPipeline
from app.core.config import settings

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch


class FakeVAD:
    def __init__(self) -> None:
        self.precision = "bf16"
        self.conversions = 0

    def float(self) -> FakeVAD:
        self.precision = "fp32"
        self.conversions += 1
        return self


def test_load_primary_uses_only_current_paraformer_models(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    models_dir = tmp_path / "models"
    paraformer_dir = models_dir / "paraformer"
    (paraformer_dir / "fsmn-vad").mkdir(parents=True)
    (paraformer_dir / "ct-punc").mkdir()
    captured: dict[str, object] = {}

    class FakeAutoModel:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)
            self.vad_model = FakeVAD()

    fake_funasr = ModuleType("funasr")
    fake_funasr.AutoModel = FakeAutoModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "funasr", fake_funasr)
    monkeypatch.setenv("BLC_MODELS_DIR", str(models_dir))

    backend = FunASRBackend(sensevoice=False, funasr_nano=False)
    loaded = backend._load_primary()

    assert isinstance(loaded, FakeAutoModel)
    assert captured["model"] == str(paraformer_dir)
    assert captured["vad_model"] == str(paraformer_dir / "fsmn-vad")
    assert captured["punc_model"] == str(paraformer_dir / "ct-punc")
    assert "spk_model" not in captured
    assert "spk_kwargs" not in captured
    assert captured["disable_update"] is True
    assert captured["vad_kwargs"] == {"fp16": False, "bf16": False}
    assert loaded.vad_model.precision == "fp32"


def test_load_nano_attaches_local_vad_with_bounded_segments(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """Engine Pack 的 Nano 必须复用本地 FSMN-VAD，并限制单段时长。"""
    models_dir = tmp_path / "models"
    (models_dir / "funasr_nano").mkdir(parents=True)
    vad_dir = models_dir / "paraformer" / "fsmn-vad"
    vad_dir.mkdir(parents=True)
    captured: dict[str, object] = {}

    class FakeAutoModel:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)
            self.vad_model = FakeVAD()

    fake_funasr = ModuleType("funasr")
    fake_funasr.AutoModel = FakeAutoModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "funasr", fake_funasr)
    monkeypatch.setenv("BLC_MODELS_DIR", str(models_dir))

    backend = FunASRBackend(sensevoice=False, funasr_nano=True)
    loaded = backend._load_funasr(for_primary=True)

    assert isinstance(loaded, FakeAutoModel)
    assert captured["model"] == str(models_dir / "funasr_nano")
    assert captured["vad_model"] == str(vad_dir)
    assert captured["vad_kwargs"] == {"max_single_segment_time": 30_000, "fp16": False, "bf16": False}
    assert loaded.vad_model.precision == "fp32"


def test_nano_generate_uses_batched_input_cache_and_sentence_timestamps(monkeypatch: MonkeyPatch) -> None:
    """Nano 推理参数应适配 VAD 长音频，并解析句级时间戳。"""
    captured: dict[str, object] = {}

    class FakeModel:
        def generate(self, **kwargs: object) -> list[dict[str, object]]:
            captured.update(kwargs)
            return [
                {
                    "text": "第一句。第二句。",
                    "sentence_info": [
                        {"text": "第一句。", "start": 1000, "end": 2500, "confidence": 0.9},
                        {"text": "第二句。", "start": 2500, "end": 4000, "confidence": 0.8},
                    ],
                }
            ]

    backend = FunASRBackend(sensevoice=False, funasr_nano=True)
    monkeypatch.setattr(backend, "_load_funasr", lambda *, for_primary=False: FakeModel())
    result = backend._transcribe_funasr_audio("normalized.wav", 10.0, 20.0, for_primary=True, audio_duration=10.0)

    assert captured == {
        "input": ["normalized.wav"],
        "cache": {},
        "batch_size_s": 0,
        "language": "中文",
        "sentence_timestamp": True,
    }
    assert [(item.start, item.end, item.text) for item in result.segments] == [
        (11.0, 12.5, "第一句。"),
        (12.5, 14.0, "第二句。"),
    ]


def test_nano_generate_receives_room_hotwords(monkeypatch: MonkeyPatch) -> None:
    """Fun-ASR-Nano 主引擎也必须接收直播间专属词典。"""
    captured: dict[str, object] = {}

    class FakeModel:
        def generate(self, **kwargs: object) -> list[dict[str, object]]:
            captured.update(kwargs)
            return [{"text": "专属词"}]

    backend = FunASRBackend(sensevoice=False, funasr_nano=True)
    monkeypatch.setattr(backend, "_load_funasr", lambda *, for_primary=False: FakeModel())
    monkeypatch.setattr("app.analysis.transcription.backends._probe_audio_duration", lambda _path: 3.0)

    backend.transcribe_funasr("normalized.wav", "查理斯, 亚运冠军")

    assert captured["hotword"] == "查理斯, 亚运冠军"


@pytest.mark.parametrize("for_primary", [False, True])
def test_online_nano_vad_precision_preserves_llm_and_reuses_model(monkeypatch: MonkeyPatch, for_primary: bool) -> None:
    captured: dict[str, object] = {}
    loaded: list[FakeAutoModel] = []

    class FakeAutoModel:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)
            self.vad_model = FakeVAD()
            self.llm_precision = "bf16"
            loaded.append(self)

        def generate(self, **kwargs: object) -> list[dict[str, object]]:
            assert self.vad_model.precision == "fp32"
            assert self.llm_precision == "bf16"
            return [{"text": "主播正在讲解当前画面的玩法。"}]

    monkeypatch.delenv("BLC_MODELS_DIR", raising=False)
    monkeypatch.setitem(sys.modules, "funasr", SimpleNamespace(AutoModel=FakeAutoModel))
    backend = FunASRBackend(sensevoice=False, funasr_nano=True)
    for _ in range(3):
        result = backend._transcribe_funasr_audio("normalized.wav", 0, 10, for_primary=for_primary, audio_duration=10)
        assert result.text == "主播正在讲解当前画面的玩法。"
    assert len(loaded) == 1
    assert loaded[0].vad_model.conversions == 1
    assert captured["vad_kwargs"] == {"max_single_segment_time": 30_000, "fp16": False, "bf16": False}
    assert model_pool.infos()[0]["active_users"] == 0


def test_missing_vad_fails_before_inference() -> None:
    with pytest.raises(RuntimeError, match="未加载.*FSMN-VAD"):
        FunASRBackend._prepare_vad_precision(SimpleNamespace(vad_model=None))


@pytest.mark.parametrize(
    ("method", "model_id", "revision"),
    [
        ("_load_primary", FunASRBackend.MODEL_ID_PRIMARY, FunASRBackend._REVISION_PRIMARY),
        ("_load_sensevoice", FunASRBackend.MODEL_ID_SENSEVOICE, FunASRBackend._REVISION_SENSEVOICE),
        ("_load_funasr", FunASRBackend.MODEL_ID_NANO, FunASRBackend._REVISION_NANO),
    ],
)
def test_online_models_use_sdk_revision_parameter(
    monkeypatch: MonkeyPatch, method: str, model_id: str, revision: str
) -> None:
    captured: dict[str, object] = {}

    class FakeAutoModel:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)
            self.vad_model = FakeVAD()

    monkeypatch.delenv("BLC_MODELS_DIR", raising=False)
    monkeypatch.setitem(sys.modules, "funasr", SimpleNamespace(AutoModel=FakeAutoModel))
    getattr(FunASRBackend(), method)()
    assert captured["model"] == model_id
    assert captured["model_revision"] == revision
    assert "revision" not in captured
    assert captured["disable_update"] is True


def test_repeated_nano_failure_reuses_fallback_models_and_can_recover(monkeypatch: MonkeyPatch) -> None:
    loaded: list[FakeAutoModel] = []
    fail_nano = [True]
    calls: list[str] = []

    class FakeAutoModel:
        def __init__(self, **kwargs: object) -> None:
            self.model_name = str(kwargs["model"])
            self.vad_model = FakeVAD()
            loaded.append(self)

        def generate(self, **kwargs: object) -> list[dict[str, object]]:
            calls.append(self.model_name)
            assert self.vad_model.precision == "fp32"
            if self.model_name == FunASRBackend.MODEL_ID_NANO and fail_nano[0]:
                raise RuntimeError("nano inference failed")
            return [{"text": "主播正在讲解当前画面的玩法。"}]

    monkeypatch.delenv("BLC_MODELS_DIR", raising=False)
    monkeypatch.setattr(settings, "asr_primary", "funasr_nano")
    monkeypatch.setitem(sys.modules, "funasr", SimpleNamespace(AutoModel=FakeAutoModel))
    monkeypatch.setattr("app.analysis.transcription.backends._probe_audio_duration", lambda _path: 10.0)
    pipeline = ASRPipeline(primary_backend=FunASRBackend(sensevoice=False))
    for _ in range(3):
        result = pipeline._transcribe_normalized("normalized.wav")
        assert result.fallback_backend == "paraformer"
        assert result.primary_error_message == "RuntimeError: nano inference failed"
        assert pipeline._last_backend_error is None
    assert len(loaded) == 2
    assert all(item.vad_model.conversions == 1 for item in loaded)
    assert len(calls) == 6
    assert all(info["active_users"] == 0 for info in model_pool.infos())
    fail_nano[0] = False
    result = pipeline._transcribe_normalized("normalized.wav")
    assert result.backend == "funasr-nano"
    assert result.fallback_backend == ""
    assert len(loaded) == 2
    assert len(calls) == 7


def test_failed_nano_initialization_releases_slot_and_traceback(monkeypatch: MonkeyPatch) -> None:
    import gc
    import weakref

    initialized = [0]
    allocations: list[weakref.ReferenceType[Allocation]] = []

    class Allocation:
        pass

    class FakeAutoModel:
        def __init__(self, **kwargs: object) -> None:
            if kwargs["model"] == FunASRBackend.MODEL_ID_NANO:
                initialized[0] += 1
                if initialized[0] == 1:
                    partial_model = Allocation()
                    allocations.append(weakref.ref(partial_model))
                    raise OSError("partial initialization failed")
            self.vad_model = FakeVAD()

        def generate(self, **kwargs: object) -> list[dict[str, object]]:
            return [{"text": "主播正在讲解当前画面的玩法。"}]

    monkeypatch.delenv("BLC_MODELS_DIR", raising=False)
    monkeypatch.setattr(settings, "asr_primary", "funasr_nano")
    monkeypatch.setitem(sys.modules, "funasr", SimpleNamespace(AutoModel=FakeAutoModel))
    monkeypatch.setattr("app.analysis.transcription.backends._probe_audio_duration", lambda _path: 10.0)
    pipeline = ASRPipeline(primary_backend=FunASRBackend(sensevoice=False))
    result = pipeline._transcribe_normalized("normalized.wav")
    assert result.fallback_backend == "paraformer"
    assert pipeline._last_backend_error is None
    gc.collect()
    assert allocations[0]() is None
    assert len(model_pool.infos()) == 1
    for _ in range(2):
        result = pipeline._transcribe_normalized("normalized.wav")
        assert result.backend == "funasr-nano"
    assert initialized[0] == 2


def test_whisper_fallback_preserves_primary_and_secondary_errors(monkeypatch: MonkeyPatch) -> None:
    from app.analysis.transcription.backends import FasterWhisperBackend
    from app.analysis.transcription.models import ASRTranscriptResult

    def nano_failed(self: FunASRBackend, audio_path: str, initial_prompt: str | None = None) -> ASRTranscriptResult:
        raise RuntimeError("nano precision error")

    def paraformer_failed(
        self: FunASRBackend, audio_path: str, initial_prompt: str | None = None
    ) -> ASRTranscriptResult:
        raise OSError("paraformer unavailable")

    def whisper_available(
        self: FasterWhisperBackend, audio_path: str, initial_prompt: str | None = None
    ) -> ASRTranscriptResult:
        return ASRTranscriptResult(text="主播正在讲解当前画面的玩法。", backend="whisper")

    monkeypatch.setattr(settings, "asr_primary", "funasr_nano")
    monkeypatch.setattr(settings, "asr_fallback_whisper", True)
    monkeypatch.setattr(FunASRBackend, "transcribe_funasr", nano_failed)
    monkeypatch.setattr(FunASRBackend, "transcribe", paraformer_failed)
    monkeypatch.setattr(FasterWhisperBackend, "transcribe", whisper_available)
    pipeline = ASRPipeline()
    result = pipeline._transcribe_normalized("normalized.wav")
    assert result.fallback_backend == "whisper"
    assert result.primary_error_message == "RuntimeError: nano precision error"
    assert result.metadata["secondary_error"] == {
        "backend": "paraformer",
        "error_type": "OSError",
        "error_message": "OSError: paraformer unavailable",
    }
    assert pipeline._last_backend_error is None


def test_all_models_failed_keeps_exception_chain_without_caching_it(monkeypatch: MonkeyPatch) -> None:
    from app.analysis.transcription.models import ASRTranscriptResult

    def failed(self: FunASRBackend, audio_path: str, initial_prompt: str | None = None) -> ASRTranscriptResult:
        raise OSError("model initialization unavailable")

    monkeypatch.setattr(settings, "asr_primary", "funasr_nano")
    monkeypatch.setattr(settings, "asr_fallback_whisper", False)
    monkeypatch.setattr(FunASRBackend, "transcribe_funasr", failed)
    monkeypatch.setattr(FunASRBackend, "transcribe", failed)
    pipeline = ASRPipeline()
    with pytest.raises(RuntimeError, match="无可用回退") as error:
        pipeline._transcribe_normalized("normalized.wav")
    assert isinstance(error.value.__cause__, OSError)
    assert str(error.value.__cause__) == "model initialization unavailable"
    assert pipeline._last_backend_error is None
