"""真实 SDK 请求端点、有限回退和下载诊断的回归测试。"""

from __future__ import annotations

import errno
import hashlib
import io
import logging
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import httpx
import pytest

if TYPE_CHECKING:
    from pytest import CaptureFixture, MonkeyPatch

PORTABLE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PORTABLE / "src"))
sys.path.insert(0, str(PORTABLE / "config"))

from blc_portable import model_download
from blc_portable.launcher import model_downloader

REVISION = "a" * 40


@pytest.mark.parametrize("redirect", [False, True])
def test_real_sdk_metadata_failure_switches_endpoint_without_same_source_retry(
    tmp_path: Path, monkeypatch: MonkeyPatch, no_download_wait: list[float], redirect: bool
) -> None:
    import huggingface_hub
    from huggingface_hub import constants
    from huggingface_hub.utils import _http

    payload = b'{"complete":true}'
    digest = hashlib.sha1(b"blob " + str(len(payload)).encode() + b"\0" + payload).hexdigest()
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert "authorization" not in request.headers
        if "/tree/" in request.url.path:
            assert REVISION in request.url.path
            return httpx.Response(
                200, json=[{"type": "file", "path": "config.json", "size": len(payload), "oid": digest}]
            )
        assert f"/resolve/{REVISION}/config.json" in request.url.path
        if request.url.host == "hf-mirror.com":
            assert request.method == "HEAD"
            return (
                httpx.Response(308, headers={"Location": "https://cdn.invalid/fixture"})
                if redirect
                else httpx.Response(200, headers={"Content-Length": str(len(payload))})
            )
        assert request.url.host == "huggingface.co"
        return httpx.Response(
            200,
            headers={"X-Repo-Commit": REVISION, "ETag": digest, "Content-Length": str(len(payload))},
            content=b"" if request.method == "HEAD" else payload,
        )

    monkeypatch.setattr(constants, "HF_HUB_OFFLINE", False)
    monkeypatch.setenv("HF_TOKEN", "must-not-leak")
    old_factory = _http._GLOBAL_CLIENT_FACTORY
    huggingface_hub.set_client_factory(lambda: httpx.Client(transport=httpx.MockTransport(respond)))
    try:
        model_downloader._download_locked_model("whisper", "huggingface", "fixture/model", tmp_path, REVISION)
    finally:
        huggingface_hub.set_client_factory(old_factory)
    assert (tmp_path / "config.json").read_bytes() == payload
    assert sum(request.url.host == "hf-mirror.com" and request.method == "HEAD" for request in requests) == 1
    assert any(
        request.url.host == "huggingface.co" and request.method == "GET" and "/resolve/" in request.url.path
        for request in requests
    )
    assert no_download_wait == []


def test_wrapped_metadata_failures_exhaust_each_source_once(no_download_wait: list[float]) -> None:
    from huggingface_hub.errors import FileMetadataError, LocalEntryNotFoundError

    calls: list[str] = []

    def download(endpoint: str) -> None:
        calls.append(endpoint)
        try:
            raise FileMetadataError("Distant resource does not seem to be on huggingface.co")
        except FileMetadataError as exc:
            raise LocalEntryNotFoundError("No local cache") from exc

    with pytest.raises(RuntimeError, match="kind=metadata.*retry=False") as caught:
        model_download.download_with_retry(
            engine="whisper",
            model="fixture/model",
            revision=REVISION,
            endpoints=model_downloader.HF_MIRRORS,
            operation=download,
        )
    assert calls == list(model_downloader.HF_MIRRORS)
    assert isinstance(caught.value.__cause__.__cause__, FileMetadataError)
    assert no_download_wait == []


@pytest.fixture
def no_download_wait(monkeypatch: MonkeyPatch) -> list[float]:
    waits: list[float] = []
    monkeypatch.setattr(model_download, "time", SimpleNamespace(sleep=waits.append))
    return waits


@pytest.fixture
def hf_requests(monkeypatch: MonkeyPatch) -> Iterator[list[httpx.Request]]:
    import huggingface_hub
    from huggingface_hub import constants
    from huggingface_hub.utils import _http

    requests: list[httpx.Request] = []
    payload = b'{"fixture":true}'
    digest = hashlib.sha1(b"blob " + str(len(payload)).encode() + b"\0" + payload).hexdigest()

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "/tree/" in request.url.path:
            return httpx.Response(
                200, json=[{"type": "file", "path": "config.json", "size": len(payload), "oid": digest}]
            )
        assert f"/resolve/{REVISION}/config.json" in request.url.path
        return httpx.Response(
            200,
            headers={"X-Repo-Commit": REVISION, "ETag": digest, "Content-Length": str(len(payload))},
            content=b"" if request.method == "HEAD" else payload,
        )

    monkeypatch.setenv("HF_TOKEN", "private-user-token")
    monkeypatch.setattr(constants, "HF_HUB_OFFLINE", False)
    old_factory = _http._GLOBAL_CLIENT_FACTORY
    huggingface_hub.set_client_factory(lambda: httpx.Client(transport=httpx.MockTransport(respond)))
    try:
        yield requests
    finally:
        huggingface_hub.set_client_factory(old_factory)


@pytest.mark.parametrize("show_progress", [False, True])
def test_preimported_sdk_uses_explicit_endpoint_for_tree_and_file_without_token(
    tmp_path: Path,
    hf_requests: list[httpx.Request],
    monkeypatch: MonkeyPatch,
    show_progress: bool,
    capsys: CaptureFixture[str],
) -> None:
    import huggingface_hub  # 已导入 SDK 后才指定端点，不能依赖环境变量的导入时机。

    assert huggingface_hub.__version__ == "1.26.0"
    monkeypatch.setenv("HF_ENDPOINT", "https://unrelated.invalid")
    monkeypatch.setenv("BLC_MODEL_PROGRESS", "1" if show_progress else "0")
    for index, endpoint in enumerate(model_downloader.HF_MIRRORS):
        target = tmp_path / str(index)
        model_downloader._download_hf_model("fixture/model", target, REVISION, endpoint)
        assert (target / "config.json").read_bytes() == b'{"fixture":true}'
    assert {request.url.host for request in hf_requests} == {"hf-mirror.com", "huggingface.co"}
    assert any("/tree/" in request.url.path for request in hf_requests)
    assert any(request.method == "GET" and "/resolve/" in request.url.path for request in hf_requests)
    assert all("authorization" not in request.headers for request in hf_requests)
    assert os.environ["HF_ENDPOINT"] == "https://unrelated.invalid"
    output = capsys.readouterr().out
    assert ("BLC_MODEL_PROGRESS=" in output) is show_progress
    if show_progress:
        assert "Reconstructing" in output and "Downloading bytes" in output


def test_first_source_timeout_falls_back_with_same_revision_and_directory(
    tmp_path: Path, monkeypatch: MonkeyPatch, no_download_wait: list[float]
) -> None:
    calls: list[tuple[str, Path, str | None, str | None]] = []

    def download(repo: str, target: Path, revision: str | None, endpoint: str | None) -> None:
        calls.append((repo, target, revision, endpoint))
        if endpoint == model_downloader.HF_MIRRORS[0]:
            raise httpx.ConnectTimeout("connect failed", request=httpx.Request("GET", endpoint + "/?signature=secret"))
        target.mkdir(exist_ok=True)
        (target / "config.json").write_bytes(b"complete")

    monkeypatch.setattr(model_downloader, "_download_hf_model", download)
    model_downloader._download_locked_model("whisper", "huggingface", "fixture/model", tmp_path, REVISION)
    assert [call[3] for call in calls] == [*model_downloader.HF_MIRRORS[:1]] * 2 + model_downloader.HF_MIRRORS[1:]
    assert all(call[:3] == ("fixture/model", tmp_path, REVISION) for call in calls)
    assert len(no_download_wait) == 1


def test_all_sources_fail_with_bounded_root_diagnostics(
    tmp_path: Path, monkeypatch: MonkeyPatch, no_download_wait: list[float], capsys: CaptureFixture[str]
) -> None:
    calls: list[str] = []

    def download(repo: str, target: Path, revision: str | None, endpoint: str) -> None:
        calls.append(endpoint)
        try:
            raise httpx.ConnectTimeout(
                "failed https://user:password@cdn.invalid/model?signature=private-signature",
                request=httpx.Request("GET", "https://cdn.invalid/model?token=private-token"),
            )
        except httpx.ConnectTimeout as exc:
            raise RuntimeError("wrapped failure") from exc

    monkeypatch.setattr(model_downloader, "_download_hf_model", download)
    with pytest.raises(RuntimeError) as caught:
        model_downloader._download_locked_model("whisper", "huggingface", "fixture/model", tmp_path, REVISION)
    assert len(calls) == 4 and len(no_download_wait) == 2
    output = str(caught.value) + capsys.readouterr().out
    for expected in (
        "whisper",
        "fixture/model",
        REVISION,
        "phase=download",
        "cdn.invalid",
        "ConnectTimeout",
        "retry=False",
    ):
        assert expected in output
    for secret in ("private-signature", "private-token", "user:password"):
        assert secret not in output
    assert caught.value.__cause__ is not None


@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EACCES, errno.EROFS])
def test_local_io_failure_never_switches_source(code: int, no_download_wait: list[float]) -> None:
    calls: list[str] = []

    def download(endpoint: str) -> None:
        calls.append(endpoint)
        raise OSError(code, "local failure")

    with pytest.raises(RuntimeError, match="kind=local_io"):
        model_download.download_with_retry(
            engine="whisper", model="a/b", revision=REVISION, endpoints=model_downloader.HF_MIRRORS, operation=download
        )
    assert len(calls) == 1 and not no_download_wait


@pytest.mark.parametrize("status,retry", [(401, False), (403, False), (404, False), (429, True), (503, True)])
def test_http_error_classification_preserves_status_and_budget(
    status: int, retry: bool, no_download_wait: list[float]
) -> None:
    calls: list[str] = []

    def download(endpoint: str) -> None:
        calls.append(endpoint)
        response = httpx.Response(status, request=httpx.Request("GET", endpoint))
        response.raise_for_status()

    with pytest.raises(RuntimeError, match=f"kind=http_{status}"):
        model_download.download_with_retry(
            engine="whisper", model="a/b", revision=REVISION, endpoints=model_downloader.HF_MIRRORS, operation=download
        )
    assert len(calls) == (4 if retry else 1)


def test_empty_model_file_is_not_complete(tmp_path: Path) -> None:
    (tmp_path / "model.bin").touch()
    assert model_downloader._missing_required_files(tmp_path, {"required_files": ["model.bin"]}) == ["model.bin"]


def test_engine_progress_does_not_claim_download_percentage(capsys: CaptureFixture[str]) -> None:
    model_downloader._print_progress(0, 4, "Whisper")
    output = capsys.readouterr().out
    assert "引擎 1/4" in output and "非文件字节进度" in output and "%" not in output


def test_launcher_process_failure_redacts_child_traceback() -> None:
    from blc_portable.launcher.main import _format_process_failure

    output = _format_process_failure(
        "failure",
        Path("python"),
        returncode=1,
        stdout="Cookie: private-cookie\nready",
        stderr="Authorization: private-auth\nhttps://u:p@proxy.invalid/?sig=private-sig",
        root_exception="ConnectTimeout https://cdn.invalid/?token=private-token",
    )
    assert "ConnectTimeout" in output and "cdn.invalid" in output
    assert all(
        secret not in output for secret in ("private-cookie", "private-auth", "private-sig", "private-token", "u:p@")
    )


@pytest.mark.parametrize("logger_name", ["huggingface_hub.utils._http", "modelscope_hub.download", "httpx"])
def test_sdk_warning_is_redacted_before_handlers_and_filter_is_restored(
    tmp_path: Path, monkeypatch: MonkeyPatch, logger_name: str
) -> None:
    import huggingface_hub

    source = logging.getLogger(logger_name)
    previous_filters = list(source.filters)
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    source.addHandler(handler)

    def fail_download(**kwargs: object) -> None:
        try:
            raise httpx.ConnectTimeout("connect https://cdn.invalid/private-model?token=private-token")
        except httpx.ConnectTimeout:
            source.warning("failed %s", "https://user:password@cdn.invalid/?signature=private-signature", exc_info=True)
            raise

    monkeypatch.setattr(huggingface_hub, "snapshot_download", fail_download)
    try:
        with pytest.raises(httpx.ConnectTimeout):
            model_download.download_hf_snapshot("fixture/model", tmp_path, REVISION, "https://hf-mirror.com")
    finally:
        source.removeHandler(handler)
    text = output.getvalue()
    assert "ConnectTimeout" in text and "cdn.invalid" in text
    assert "private-" not in text and "user:password" not in text
    assert source.filters == previous_filters


@pytest.mark.parametrize("mode", ["complete", "missing", "size", "hash", "empty", "unverifiable", "escape"])
def test_modelscope_manifest_rejects_silent_partial_download(
    tmp_path: Path, monkeypatch: MonkeyPatch, mode: str
) -> None:
    from importlib import import_module, metadata

    from modelscope_hub.api import HubApi
    from modelscope_hub.types import FileInfo

    assert metadata.version("modelscope-hub") == "0.2.0"
    payload = b"valid model"
    calls: list[tuple[str, str | None, bool]] = []
    snapshot_calls: list[dict[str, object]] = []
    files = [
        FileInfo(path="weights.bin", size=len(payload), sha256=hashlib.sha256(payload).hexdigest(), type="blob"),
        FileInfo(path=".gitattributes", size=0, sha256=hashlib.sha256(b"").hexdigest(), type="blob"),
    ]
    if mode == "empty":
        files.clear()
    elif mode == "unverifiable":
        files[0].size = 0  # 真实 SDK 对缺失 Size 的输出。
        files[0].sha256 = None
    elif mode == "escape":
        files[0].path = "../outside.bin"

    def listing(self: HubApi, repo_id: str, repo_type: str, *, revision: str | None, recursive: bool) -> list[FileInfo]:
        assert repo_type == "model"
        calls.append((repo_id, revision, recursive))
        return files

    def partial_snapshot(**kwargs: object) -> str:
        snapshot_calls.append(kwargs)
        (tmp_path / ".gitattributes").write_bytes(b"")
        if mode != "missing":
            (tmp_path / "weights.bin").write_bytes(
                b"short" if mode == "size" else b"broken data" if mode == "hash" else payload
            )
        # 锁定 SDK 在单文件失败时可能仍返回目录，适配层必须拒绝。
        return str(tmp_path)

    monkeypatch.setattr(HubApi, "list_repo_files", listing)
    monkeypatch.setattr(import_module("modelscope_hub.compat.snapshot_download"), "snapshot_download", partial_snapshot)
    if mode == "complete":
        model_download.download_ms_snapshot("fixture/sub-model", tmp_path, REVISION, "https://www.modelscope.cn")
    else:
        with pytest.raises(RuntimeError, match="phase=verify"):
            model_download.download_ms_snapshot("fixture/sub-model", tmp_path, REVISION, "https://www.modelscope.cn")
    assert calls == [("fixture/sub-model", REVISION, True)]
    if mode in {"empty", "unverifiable", "escape"}:
        assert not snapshot_calls
    else:
        assert snapshot_calls == [
            {
                "model_id": "fixture/sub-model",
                "revision": REVISION,
                "local_dir": str(tmp_path),
                "endpoint": "https://www.modelscope.cn",
            }
        ]


@pytest.mark.parametrize(
    "text",
    [
        "{'Authorization': 'Bearer private credential'}",
        '{"token":"private credential"}',
        'password="private credential"',
        "{'Cookie': 'custom=private credential'}",
        "sk-privatecredential hf_privatecredential",
    ],
)
def test_quoted_diagnostic_credentials_are_redacted(text: str) -> None:
    from app.core.sanitize import sanitize_diagnostic

    assert "private" not in model_download.redact_diagnostic(text)
    assert "private" not in sanitize_diagnostic(text)


@pytest.mark.parametrize("error", [httpx.UnsupportedProtocol("bad protocol"), httpx.LocalProtocolError("bad proxy")])
def test_protocol_configuration_does_not_retry(error: Exception, no_download_wait: list[float]) -> None:
    calls: list[str] = []

    def download(endpoint: str) -> None:
        calls.append(endpoint)
        raise error

    with pytest.raises(RuntimeError, match="kind=configuration"):
        model_download.download_with_retry(
            engine="whisper", model="a/b", revision=REVISION, endpoints=model_downloader.HF_MIRRORS, operation=download
        )
    assert len(calls) == 1 and not no_download_wait
