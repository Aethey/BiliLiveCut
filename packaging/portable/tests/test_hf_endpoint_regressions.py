"""用真实 Hugging Face SDK 验证端点、重定向与协议失败边界。"""

from __future__ import annotations

import hashlib
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest

if TYPE_CHECKING:
    from pytest import MonkeyPatch

PORTABLE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PORTABLE / "src"))

from blc_portable import model_download
from blc_portable.launcher import model_downloader

REVISION = "a" * 40
PAYLOAD = b'{"complete":true}'
ETAG = hashlib.sha1(b"blob " + str(len(PAYLOAD)).encode() + b"\0" + PAYLOAD).hexdigest()
Responder = Callable[[httpx.Request], httpx.Response]
InstallClient = Callable[[Responder], list[httpx.Request]]


@pytest.fixture(autouse=True)
def isolated_hf_endpoint(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("HF_ENDPOINT", raising=False)


@pytest.fixture
def install_hf_client(monkeypatch: MonkeyPatch) -> Iterator[InstallClient]:
    import huggingface_hub
    from huggingface_hub import constants
    from huggingface_hub.utils import _http

    monkeypatch.setattr(constants, "HF_HUB_OFFLINE", False)
    monkeypatch.setenv("HF_TOKEN", "private-must-not-leak")
    previous = _http._GLOBAL_CLIENT_FACTORY

    def install(responder: Responder) -> list[httpx.Request]:
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            assert "authorization" not in request.headers
            assert len(requests) < 100, "redirect/retry budget must be finite"
            return responder(request)

        # Match the SDK's default client: API requests follow redirects, while
        # file metadata uses the SDK's own bounded Hub-only redirect follower.
        huggingface_hub.set_client_factory(
            lambda: httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=True)
        )
        return requests

    try:
        yield install
    finally:
        huggingface_hub.set_client_factory(previous)


def complete_response(request: httpx.Request) -> httpx.Response:
    if "/tree/" in request.url.path:
        assert REVISION in request.url.path
        return httpx.Response(200, json=[{"type": "file", "path": "config.json", "size": len(PAYLOAD), "oid": ETAG}])
    assert REVISION in request.url.path
    return httpx.Response(
        200,
        headers={"X-Repo-Commit": REVISION, "ETag": ETAG, "Content-Length": str(len(PAYLOAD))},
        content=b"" if request.method == "HEAD" else PAYLOAD,
    )


def test_absolute_hub_redirect_and_relative_query_are_followed_by_real_sdk(
    tmp_path: Path, install_hf_client: InstallClient
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "hf-mirror.com":
            return httpx.Response(308, headers={"Location": str(request.url.copy_with(host="huggingface.co"))})
        assert request.url.host == "huggingface.co"
        if "/resolve/" in request.url.path:
            return httpx.Response(
                307, headers={"Location": f"/api/resolve-cache/models/fixture/model/{REVISION}/config.json?etag={ETAG}"}
            )
        if "/resolve-cache/" in request.url.path:
            assert request.url.params["etag"] == ETAG
        return complete_response(request)

    requests = install_hf_client(respond)
    model_downloader._download_locked_model("whisper", "huggingface", "fixture/model", tmp_path, REVISION)
    assert (tmp_path / "config.json").read_bytes() == PAYLOAD
    assert any(r.method == "HEAD" and "/resolve-cache/" in r.url.path for r in requests)
    assert any(r.method == "GET" and "/resolve-cache/" in r.url.path for r in requests)


def test_custom_source_works_without_contacting_blocked_default_hosts(
    tmp_path: Path, install_hf_client: InstallClient, monkeypatch: MonkeyPatch
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host != "independent.example":
            raise httpx.ConnectError("default host is blocked", request=request)
        assert request.url.path.startswith("/hub/")
        return complete_response(request)

    requests = install_hf_client(respond)  # SDK was imported before the override.
    monkeypatch.setenv("HF_ENDPOINT", "https://independent.example/hub/")
    model_downloader._download_locked_model("whisper", "huggingface", "fixture/model", tmp_path, REVISION)
    assert (tmp_path / "config.json").read_bytes() == PAYLOAD
    assert {r.url.host for r in requests} == {"independent.example"}
    assert model_download.HF_MIRRORS == ["https://hf-mirror.com", "https://huggingface.co"]


def test_mirror_delegation_does_not_hide_blocked_official_host(
    tmp_path: Path, install_hf_client: InstallClient, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("HF_ENDPOINT", "https://hf-mirror.com")
    monkeypatch.setattr(model_download.time, "sleep", lambda seconds: None)

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "hf-mirror.com":
            # Keep the listing available so the actual HEAD delegation is tested.
            if "/tree/" in request.url.path:
                return complete_response(request)
            return httpx.Response(308, headers={"Location": str(request.url.copy_with(host="huggingface.co"))})
        raise httpx.ConnectError("official unavailable", request=request)

    requests = install_hf_client(respond)
    with pytest.raises(
        RuntimeError, match=r"failed_host=<host:huggingface\.co>.*kind=network.*source=1/1.*retry=False"
    ):
        model_downloader._download_locked_model("whisper", "huggingface", "fixture/model", tmp_path, REVISION)
    assert not (tmp_path / "config.json").exists()
    # Each of our two attempts performs one fast metadata probe, then the SDK
    # retries metadata once with its five-retry backoff when no cache exists.
    assert len([r for r in requests if r.method == "HEAD" and r.url.host == "hf-mirror.com"]) == 4
    assert len([r for r in requests if r.method == "HEAD" and r.url.host == "huggingface.co"]) == 14


@pytest.mark.parametrize("failure", ["missing_length", "foreign_redirect", "redirect_loop"])
def test_metadata_and_redirect_failures_do_not_create_completed_files(
    tmp_path: Path, install_hf_client: InstallClient, monkeypatch: MonkeyPatch, failure: str
) -> None:
    monkeypatch.setenv("HF_ENDPOINT", "https://independent.example")

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "independent.example", "metadata must not follow an arbitrary host"
        if "/tree/" in request.url.path:
            return complete_response(request)
        assert request.method == "HEAD"
        if failure == "missing_length":
            return httpx.Response(200, headers={"X-Repo-Commit": REVISION, "ETag": ETAG}, stream=httpx.ByteStream(b""))
        target = "https://untrusted.example/file" if failure == "foreign_redirect" else str(request.url)
        return httpx.Response(308, headers={"Location": target})

    requests = install_hf_client(respond)
    with pytest.raises(RuntimeError, match="retry=False") as caught:
        model_downloader._download_locked_model("whisper", "huggingface", "fixture/model", tmp_path, REVISION)
    if failure != "redirect_loop":
        assert "kind=metadata" in str(caught.value)
        assert "HF_ENDPOINT" in str(caught.value)
    else:
        assert "TooManyRedirects" in str(caught.value)
    assert not (tmp_path / "config.json").exists()
    assert not any(r.method == "GET" and "/resolve/" in r.url.path for r in requests)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://mirror.example",
        "file:///private",
        "https://",
        "https://host:0",
        "https://host:65536",
        "https://host:notaport",
        "https://user:secret@host",
        "https://host?token=secret",
        "https://host#secret",
        "https://host\\evil",
        "https://host\nAuthorization: secret",
        "https://host/../escape",
        "https://[invalid",
        " https://host",
        "https://host/white space",
    ],
)
def test_invalid_endpoint_is_rejected_before_network_without_echoing_credentials(
    endpoint: str, monkeypatch: MonkeyPatch, tmp_path: Path, install_hf_client: InstallClient
) -> None:
    requests = install_hf_client(complete_response)
    monkeypatch.setenv("HF_ENDPOINT", endpoint)
    with pytest.raises(ValueError, match="HF_ENDPOINT") as caught:
        model_downloader._download_locked_model("whisper", "huggingface", "fixture/model", tmp_path, REVISION)
    assert "secret" not in str(caught.value)
    assert not requests


def test_empty_override_returns_independent_default_list(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("HF_ENDPOINT", "")
    selected = model_download.get_hf_endpoints()
    assert selected == model_download.HF_MIRRORS
    selected.clear()
    assert len(model_download.get_hf_endpoints()) == 2


def test_explicit_download_argument_precedes_environment(
    tmp_path: Path, monkeypatch: MonkeyPatch, install_hf_client: InstallClient
) -> None:
    requests = install_hf_client(complete_response)
    monkeypatch.setenv("HF_ENDPOINT", "https://environment.example")
    model_download.download_hf_snapshot("fixture/model", tmp_path, REVISION, "https://explicit.example")
    assert {r.url.host for r in requests} == {"explicit.example"}


def test_engine_pack_builder_uses_runtime_endpoint(
    tmp_path: Path, monkeypatch: MonkeyPatch, install_hf_client: InstallClient
) -> None:
    from blc_portable.engine_pack import builder

    requests = install_hf_client(complete_response)
    monkeypatch.setenv("HF_ENDPOINT", "https://independent.example")
    monkeypatch.setattr(
        builder,
        "_get_engines_for_build",
        lambda: [
            {
                "engine_id": "whisper",
                "engine_name": "fixture",
                "target_path": "whisper",
                "hub": "huggingface",
                "repo_id": "fixture/model",
                "revision": REVISION,
            }
        ],
    )
    builder.download_real_models(tmp_path)
    assert (tmp_path / "whisper/config.json").read_bytes() == PAYLOAD
    assert {r.url.host for r in requests} == {"independent.example"}


def test_engine_pack_cache_downloader_uses_runtime_endpoint(
    tmp_path: Path, monkeypatch: MonkeyPatch, install_hf_client: InstallClient
) -> None:
    from blc_portable.engine_pack import downloader

    requests = install_hf_client(complete_response)
    monkeypatch.setenv("HF_ENDPOINT", "https://independent.example")
    monkeypatch.setattr(downloader, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(downloader, "STATE_FILE", tmp_path / "download_state.json")
    assert downloader.download_engine(
        {
            "engine_id": "whisper",
            "engine_name": "fixture",
            "cache_dir": "whisper",
            "hub": "huggingface",
            "model_id": "fixture/model",
            "revision": REVISION,
            "required_files": ["config.json"],
        }
    )
    assert (tmp_path / "whisper/config.json").read_bytes() == PAYLOAD
    assert {r.url.host for r in requests} == {"independent.example"}
    assert downloader.load_state()["downloaded"] == ["whisper"]
