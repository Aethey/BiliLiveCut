"""远程文件转写的真实序列化、时间轴、错误边界与本地回退。"""

from __future__ import annotations

import io
import json
import wave
from collections.abc import Callable
from email import policy
from email.parser import BytesParser
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from app.analysis.model_pool import model_pool, preload_models
from app.analysis.transcription.backends import FasterWhisperBackend
from app.analysis.transcription.models import ASRTranscriptResult
from app.analysis.transcription.pipeline import ASRPipeline
from app.analysis.transcription.process_pool import _roles
from app.analysis.transcription.remote import OpenAICompatibleBackend, RemoteTranscriptionError
from app.core.config import Settings, settings
from app.core.runtime_settings import effective_settings
from app.core.sanitize import safe_exception_summary


@pytest.fixture()
def remote_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "asr_remote_base_url", "https://asr.example.test/v1")
    monkeypatch.setattr(settings, "asr_remote_model", "fixture-asr")
    monkeypatch.setattr(settings, "asr_remote_api_key", "fixture-only-credential")
    monkeypatch.setattr(settings, "asr_primary", "openai_compatible")
    monkeypatch.setattr(settings, "asr_fallback_whisper", False)


def _transport(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]) -> None:
    client = httpx.Client

    def factory(**kwargs: object) -> httpx.Client:
        return client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "Client", factory)


def _response(payload: object, status: int = 200, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, headers=headers, stream=httpx.ByteStream(json.dumps(payload).encode()))


def _parts(request: httpx.Request) -> dict[str, list[bytes]]:
    message = BytesParser(policy=policy.default).parsebytes(
        f"Content-Type: {request.headers['content-type']}\r\nMIME-Version: 1.0\r\n\r\n".encode() + request.read()
    )
    parts: dict[str, list[bytes]] = {}
    for part in message.iter_parts():
        parts.setdefault(part.get_param("name", header="content-disposition"), []).append(part.get_payload(decode=True))
    return parts


def test_multipart_json_transcription_preserves_unknown_confidence(
    remote_config: None, pcm_wav: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert str(request.url) == "https://asr.example.test/v1/audio/transcriptions"
        assert request.headers["authorization"] == "Bearer fixture-only-credential"
        parts = _parts(request)
        assert parts["model"] == [b"fixture-asr"] and parts["response_format"] == [b"json"]
        assert parts["prompt"] == ["房间热词".encode()]
        assert "timestamp_granularities[]" not in parts and "language" not in parts
        with wave.open(io.BytesIO(parts["file"][0]), "rb") as audio:
            assert audio.getnframes() == 133376 and audio.getframerate() == 16000
        return _response({"text": "这是远程服务返回的转写结果。", "language": "zh"})

    _transport(monkeypatch, handler)
    result = ASRPipeline().transcribe(str(pcm_wav), "房间热词")
    assert len(requests) == 1 and result.backend == "openai_compatible"
    assert result.model_id == "fixture-asr" and result.model_source == "remote"
    assert result.audio_duration == 8.336 and result.language == "zh"
    assert result.text == result.final_text == "这是远程服务返回的转写结果。"
    assert result.segments[0].metadata["timestamp_source"] == "estimated_chunk"
    assert result.segments[0].end == 8.336 and result.segments[0].words == []
    assert result.segments[0].raw_confidence is None and not result.segments[0].confidence_available
    assert not model_pool.infos()


def test_verbose_chunks_shift_provider_word_timestamps(
    remote_config: None, pcm_wav: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "asr_remote_chunk_s", 2)
    monkeypatch.setattr(settings, "asr_remote_response_format", "verbose_json")
    monkeypatch.setattr(settings, "asr_remote_language", "zh")
    durations: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        parts = _parts(request)
        assert parts["timestamp_granularities[]"] == [b"word", b"segment"]
        assert parts["language"] == [b"zh"]
        with wave.open(io.BytesIO(parts["file"][0]), "rb") as audio:
            durations.append(audio.getnframes() / audio.getframerate())
        return _response(
            {
                "text": "你好",
                "words": [{"word": "你好", "start": 0.1, "end": 0.2}],
                "segments": [{"text": "你好", "start": 0.0, "end": 0.3, "avg_logprob": -0.2}],
            }
        )

    _transport(monkeypatch, handler)
    result = OpenAICompatibleBackend().transcribe(str(pcm_wav))
    assert durations == [2, 2, 2, 2, 0.336]
    assert [segment.start for segment in result.segments] == [0, 2, 4, 6, 8]
    assert [segment.words[0].start for segment in result.segments] == [0.1, 2.1, 4.1, 6.1, 8.1]
    assert result.segments[-1].words[0].end == pytest.approx(8.2)
    assert all(segment.normalized_confidence is None for segment in result.segments)


@pytest.mark.parametrize("status", [302, 401, 403, 429, 500])
def test_http_failures_never_retry_follow_redirect_or_echo_response(
    remote_config: None, pcm_wav: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return _response({"error": "fixture-only-credential"}, status, {"location": "https://other.invalid/steal"})

    _transport(monkeypatch, handler)
    with pytest.raises(RemoteTranscriptionError, match=f"HTTP {status}") as caught:
        ASRPipeline().transcribe(str(pcm_wav))
    assert len(calls) == 1
    assert "fixture-only-credential" not in safe_exception_summary(caught.value)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"text": ["fixture-only-credential"]},
        {"text": "", "words": [{"word": "contradiction", "start": 0, "end": 1}]},
        {"text": "valid", "words": [{"word": "bad", "start": 4, "end": 1}]},
        {"text": "valid", "words": [{"word": "bad", "start": 9, "end": 10}]},
        {"text": "valid", "segments": [{"text": "bad", "start": -1, "end": 2}]},
        {"text": "valid", "words": [{"word": "bad", "start": 0, "end": float("nan")}]},
        {"text": "valid", "words": [{"word": "one", "start": 2, "end": 3}, {"word": "two", "start": 1, "end": 2}]},
    ],
)
def test_invalid_response_and_timestamps_fail_without_leaking_values(
    remote_config: None, pcm_wav: Path, monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    _transport(monkeypatch, lambda request: _response(payload))
    with pytest.raises(RemoteTranscriptionError) as caught:
        OpenAICompatibleBackend().transcribe(str(pcm_wav))
    assert "fixture-only-credential" not in safe_exception_summary(caught.value)


def test_response_size_is_bounded(remote_config: None, pcm_wav: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _transport(monkeypatch, lambda request: _response({"text": "x" * (2 * 1024 * 1024)}))
    with pytest.raises(RemoteTranscriptionError, match="2 MiB"):
        OpenAICompatibleBackend().transcribe(str(pcm_wav))


def test_late_chunk_failure_discards_partial_result(
    remote_config: None, pcm_wav: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "asr_remote_chunk_s", 2)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _response({"text": "只有第一块成功"}, 200 if calls == 1 else 500)

    _transport(monkeypatch, handler)
    with pytest.raises(RemoteTranscriptionError, match="500"):
        ASRPipeline().transcribe(str(pcm_wav))
    assert calls == 2


def test_empty_response_requires_real_no_speech_evidence(
    remote_config: None, pcm_wav: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _transport(monkeypatch, lambda request: _response({"text": ""}))
    result = ASRPipeline().transcribe(str(pcm_wav))
    assert result.result_type == "no_speech" and result.text == ""
    import faster_whisper.vad

    assert result.metadata["vad_speech_segments"] == 0
    monkeypatch.setattr(
        faster_whisper.vad, "get_speech_timestamps", lambda *args, **kwargs: [{"start": 0, "end": 16000}]
    )
    with pytest.raises(RemoteTranscriptionError, match="含语音"):
        ASRPipeline().transcribe(str(pcm_wav))


def test_local_fallback_keeps_remote_failure_and_primary_slots(
    remote_config: None, pcm_wav: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "asr_fallback_whisper", True)
    _transport(monkeypatch, lambda request: _response({"error": "private"}, 503))

    def whisper(self: FasterWhisperBackend, audio_path: str, initial_prompt: str | None = None) -> ASRTranscriptResult:
        return ASRTranscriptResult(text="本地回退识别的完整结果", backend="whisper", audio_duration=8.336)

    monkeypatch.setattr(FasterWhisperBackend, "transcribe", whisper)
    result = ASRPipeline().transcribe(str(pcm_wav))
    assert result.final_text_source == "fallback" and result.fallback_backend == "whisper"
    assert result.primary_backend == "openai_compatible" and result.primary_status == "failed"
    assert "503" in result.primary_error_message and result.primary_error_type == "RemoteTranscriptionError"
    assert _roles(effective_settings()) == ("fallback", "primary")


def test_remote_preload_does_not_create_local_models(remote_config: None) -> None:
    assert _roles(effective_settings()) == ("primary",)
    preload_models(refresh=False)
    assert not model_pool.infos()


@pytest.mark.parametrize(
    "address",
    [
        "ftp://example.org",
        "https://user:secret@example.org/v1",
        "https://example.org/v1?key=secret",
        "https://example.org/#secret",
        "https://example.org:0",
        "https://example.org:wrong",
        "https://example.org/\nheader",
        "https://example.org\\evil",
        "https://",
    ],
)
def test_remote_configuration_rejects_unsafe_or_ambiguous_urls(address: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, asr_remote_base_url=address)


def test_remote_configuration_requires_explicit_model_and_rejects_header_injection() -> None:
    with pytest.raises(ValidationError, match="asr_remote_model"):
        Settings(_env_file=None, asr_primary="openai_compatible")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, asr_remote_api_key="key\r\nInjected: yes")
    valid = Settings(
        _env_file=None,
        asr_primary="openai_compatible",
        asr_remote_base_url="http://127.0.0.1:8000/v1/",
        asr_remote_model="user-chosen-model",
    )
    assert valid.asr_remote_base_url == "http://127.0.0.1:8000/v1"


def test_timeout_preserves_cause_without_retry(
    remote_config: None, pcm_wav: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("read deadline", request=request)

    _transport(monkeypatch, handler)
    with pytest.raises(RemoteTranscriptionError) as caught:
        ASRPipeline().transcribe(str(pcm_wav))
    assert calls == 1 and isinstance(caught.value.__cause__, httpx.ReadTimeout)


def test_total_response_budget_prevents_unbounded_chunk_accumulation(
    remote_config: None, pcm_wav: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.analysis.transcription.remote as remote

    monkeypatch.setattr(settings, "asr_remote_chunk_s", 2)
    payload = {"text": "每一块都有效但总量必须有界"}
    monkeypatch.setattr(remote, "_MAX_TOTAL_RESPONSE_BYTES", len(json.dumps(payload).encode()) + 1)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _response(payload)

    _transport(monkeypatch, handler)
    with pytest.raises(RemoteTranscriptionError, match="累计响应"):
        OpenAICompatibleBackend().transcribe(str(pcm_wav))
    assert calls == 2


def test_anonymous_service_omits_authorization(
    remote_config: None, pcm_wav: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "asr_remote_api_key", "")

    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        return _response({"text": "无鉴权自建接口也可以转写"})

    _transport(monkeypatch, handler)
    assert OpenAICompatibleBackend().transcribe(str(pcm_wav)).text == "无鉴权自建接口也可以转写"


def test_valid_word_timestamps_without_segments_are_preserved(
    remote_config: None, pcm_wav: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _transport(
        monkeypatch, lambda request: _response({"text": "你好", "words": [{"word": "你好", "start": 0.2, "end": 1.2}]})
    )
    result = OpenAICompatibleBackend().transcribe(str(pcm_wav))
    assert result.segments[0].metadata["timestamp_source"] == "provider_words"
    assert result.segments[0].words[0].start == 0.2 and result.segments[0].words[0].end == 1.2


def test_overlapping_provider_segments_cannot_duplicate_words_without_bound(
    remote_config: None, pcm_wav: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _transport(
        monkeypatch,
        lambda request: _response(
            {
                "text": "重叠片段",
                "segments": [{"text": "one", "start": 0, "end": 3}, {"text": "two", "start": 0, "end": 3}],
                "words": [{"word": "word", "start": 1, "end": 2}],
            }
        ),
    )
    with pytest.raises(RemoteTranscriptionError, match="时间戳"):
        OpenAICompatibleBackend().transcribe(str(pcm_wav))
