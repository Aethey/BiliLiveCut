"""扫码协议与后台任务的离线验收。"""

from __future__ import annotations

import asyncio
import base64
from unittest.mock import AsyncMock
from xml.etree import ElementTree

import httpx
import pytest

from app.sources.bilibili.login import QrLoginError, QrState, WebQrLogin
from app.web.qr_login import QrLoginManager

QR = {"url": "https://passport.bilibili.com/scan?qrcode_key=test-key", "qrcode_key": "test-key"}


def success(session: str = "session%2Cencoded") -> dict[str, object]:
    return {
        "code": 0,
        "url": f"https://passport.bilibili.com/callback?SESSDATA={session}&bili_jct=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa&DedeUserID=123",
        "refresh_token": "private-refresh",
    }


@pytest.mark.parametrize(
    "session,encoded", [("session%2Cencoded", "session%2Cencoded"), ("session,raw", "session%2Craw")]
)
async def test_web_qr_sequence_and_credentials(session: str, encoded: str) -> None:
    events = iter([{"code": 86101}, {"code": 86090}, success(session)])
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/nav"):
            return httpx.Response(200, json={"code": 0, "data": {"isLogin": True, "mid": 123}})
        return httpx.Response(
            200, json={"code": 0, "data": QR if request.url.path.endswith("generate") else next(events)}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        login = WebQrLogin(client)
        with pytest.raises(QrLoginError):
            login.get_credential()
        await login.generate_qrcode()
        assert login.url == QR["url"]
        assert await login.check_state() == QrState.SCAN
        assert await login.check_state() == QrState.CONF
        with pytest.raises(QrLoginError):
            login.get_credential()
        assert await login.check_state() == QrState.VERIFY
        with pytest.raises(QrLoginError):
            login.get_credential()
        await login.validate_credential()
        assert login.get_credential() == {
            "SESSDATA": encoded,
            "bili_jct": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "ac_time_value": "private-refresh",
            "DedeUserID": "123",
        }
        copy = login.get_credential()
        copy["SESSDATA"] = "tampered"
        assert login.get_credential()["SESSDATA"] == encoded
    assert all(request.method == "GET" for request in requests)
    assert dict(requests[0].url.params) == {"source": "main-fe-header"}
    assert all(dict(request.url.params) == {"qrcode_key": "test-key"} for request in requests[1:-1])
    assert all(request.headers["referer"] == "https://www.bilibili.com" for request in requests)
    assert requests[-1].url == "https://api.bilibili.com/x/web-interface/nav"
    assert f"SESSDATA={encoded}" in requests[-1].headers["cookie"]


@pytest.mark.parametrize(
    "body,status", [({"code": -412, "data": {}}, 200), ({"code": 0, "data": None}, 200), ({}, 503)]
)
async def test_protocol_failures_do_not_include_private_payload(body: dict[str, object], status: int) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json=body))
    ) as client:
        login = WebQrLogin(client)
        with pytest.raises(QrLoginError) as error:
            await login.generate_qrcode()
        assert "qrcode_key" not in str(error.value)
        with pytest.raises(QrLoginError):
            login.get_credential()


async def test_expired_qr_is_not_automatically_replaced() -> None:
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(
            200, json={"code": 0, "data": QR if request.url.path.endswith("generate") else {"code": 86038}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        login = WebQrLogin(client)
        await login.generate_qrcode()
        assert await login.check_state() == QrState.TIMEOUT
        assert len(requests) == 2
        with pytest.raises(QrLoginError):
            login.get_credential()


@pytest.mark.parametrize("event", [{"code": 999}, {"code": 0, "url": "https://invalid/", "refresh_token": "private"}])
async def test_unusable_completion_does_not_produce_cookie(event: dict[str, object]) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": QR if request.url.path.endswith("generate") else event})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        login = WebQrLogin(client)
        await login.generate_qrcode()
        with pytest.raises(QrLoginError):
            await login.check_state()
        with pytest.raises(QrLoginError):
            login.get_credential()


async def test_callback_missing_required_cookie_fields_is_rejected() -> None:
    data = {"code": 0, "url": "https://passport.bilibili.com/callback?SESSDATA=session", "refresh_token": ""}

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": QR if request.url.path.endswith("generate") else data})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        login = WebQrLogin(client)
        await login.generate_qrcode()
        with pytest.raises(QrLoginError):
            await login.check_state()
        with pytest.raises(QrLoginError):
            login.get_credential()


@pytest.mark.parametrize(
    "domain,query,uid,valid",
    [
        (".bilibili.com", "", 123, True),
        ("passport.bilibili.com", "", 123, True),
        (".bilibili.com.evil.test", "", 123, False),
        (".bilibili.com", "&SESSDATA=", 123, False),
        (".bilibili.com", "&bili_jct=invalid", 123, False),
        (".bilibili.com", "", 456, False),
    ],
)
async def test_header_credentials_require_trusted_domain_and_account_validation(
    domain: str, query: str, uid: int, valid: bool
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/nav"):
            return httpx.Response(200, json={"code": 0, "data": {"isLogin": True, "mid": uid}})
        data = (
            QR
            if request.url.path.endswith("generate")
            else {
                "code": 0,
                "url": "https://passport.biligame.com/crossDomain?gourl=https%3A%2F%2Fwww.bilibili.com" + query,
            }
        )
        return httpx.Response(200, json={"code": 0, "data": data})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        login = WebQrLogin(client)
        await login.generate_qrcode()
        for key, value in {
            "SESSDATA": "session%2Cencoded",
            "bili_jct": "a" * 32,
            "DedeUserID": "123",
        }.items():
            client.cookies.set(key, value, domain=domain)
        if valid:
            assert await login.check_state() == QrState.VERIFY
            with pytest.raises(QrLoginError):
                login.get_credential()
            await login.validate_credential()
            assert login.get_credential()["DedeUserID"] == "123"
        else:
            with pytest.raises(QrLoginError):
                await login.check_state()
                await login.validate_credential()
            with pytest.raises(QrLoginError):
                login.get_credential()


def test_svg_qr_needs_no_display_or_pillow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    image = QrLoginManager._render(QR["url"])
    svg = ElementTree.fromstring(base64.b64decode(image.partition(",")[2]))
    assert svg.tag == "{http://www.w3.org/2000/svg}svg"
    assert any(node.tag.endswith("path") for node in svg)
    assert "test-key" not in image


@pytest.mark.parametrize("last", [QrState.DONE, QrState.TIMEOUT])
async def test_manager_saves_only_after_confirmation(last: QrState, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.web import login_handler
    from app.web import qr_login as service

    saved = []
    generate = AsyncMock()
    check = AsyncMock(side_effect=[QrState.SCAN, QrState.CONF, QrState.VERIFY if last == QrState.DONE else last])
    monkeypatch.setattr(service.WebQrLogin, "validate_credential", AsyncMock())
    monkeypatch.setattr(service.WebQrLogin, "generate_qrcode", generate)
    monkeypatch.setattr(service.WebQrLogin, "check_state", check)
    monkeypatch.setattr(
        service.WebQrLogin,
        "get_credential",
        lambda self: {
            "SESSDATA": "session%2Cencoded",
            "bili_jct": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "DedeUserID": "123",
            "ac_time_value": "private-refresh",
        },
    )
    monkeypatch.setattr(login_handler, "_save_cookie", saved.append)
    manager = QrLoginManager()
    manager.poll_interval = 0
    token = manager.start("owner")
    task = manager.get(token, "owner")
    assert task is not None and task.worker is not None
    assert manager.get(token, "another-owner") is None
    await task.worker
    assert task.status == last and not task.image
    assert bool(saved) == (last == QrState.DONE)
    if saved:
        assert "ac_time_value=private-refresh" in saved[0]
    await manager.stop()


async def test_cancel_closes_http_and_does_not_save(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.web import login_handler
    from app.web import qr_login as service

    started = asyncio.Event()
    closed = asyncio.Event()
    saved = []

    async def wait(self: WebQrLogin) -> QrState:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()
        return QrState.DONE

    monkeypatch.setattr(service.WebQrLogin, "generate_qrcode", AsyncMock())
    monkeypatch.setattr(service.WebQrLogin, "check_state", wait)
    monkeypatch.setattr(login_handler, "_save_cookie", saved.append)
    manager = QrLoginManager()
    token = manager.start("owner")
    try:
        await asyncio.wait_for(started.wait(), timeout=3)
        with pytest.raises(QrLoginError):
            manager.start("owner")
        assert not await manager.cancel(token, "intruder")
        assert await manager.cancel(token, "owner")
        assert closed.is_set() and not saved
        assert manager.get(token, "owner").status == "cancelled"
    finally:
        await manager.stop()
    assert not manager.tasks


async def test_manager_hides_network_credentials_and_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.web import qr_login as service

    monkeypatch.setattr(
        service.WebQrLogin,
        "generate_qrcode",
        AsyncMock(side_effect=RuntimeError("SESSDATA=private&refresh_token=secret")),
    )
    manager = QrLoginManager()
    token = manager.start("owner")
    task = manager.get(token, "owner")
    await task.worker
    assert task.status == "error" and not task.image
    assert "private" not in task.error and "secret" not in task.error
    manager.retention_seconds = -1
    second = manager.start("owner")
    assert token not in manager.tasks and second != token
    await manager.stop()
