"""扫码协议异常与真实后台生命周期的安全回归。"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable

import httpx
import pytest

from app.sources.bilibili.login import QrLoginError, QrState, WebQrLogin
from app.web.qr_login import QrLoginManager

QR = {"url": "https://account.bilibili.com/h5/account-h5/auth/scan-web?qrcode_key=fixture", "qrcode_key": "fixture"}
CALLBACK = "https://passport.bilibili.com/callback?SESSDATA=session%2Cvalue&bili_jct=" + "a" * 32 + "&DedeUserID=123"
SUCCESS = {"code": 0, "url": CALLBACK, "refresh_token": "refresh-fixture"}


@pytest.mark.parametrize(
    "event",
    [
        {**SUCCESS, "code": 999},
        {**SUCCESS, "code": False},
        {**SUCCESS, "code": []},
        {**SUCCESS, "url": CALLBACK.replace("https:", "http:")},
        {**SUCCESS, "url": CALLBACK.replace("passport.bilibili.com", "passport.bilibili.com.evil.test")},
        {**SUCCESS, "url": CALLBACK.replace("passport.bilibili.com", "evil@passport.bilibili.com")},
        {**SUCCESS, "url": CALLBACK.replace("passport.bilibili.com", "passport.bilibili.com:444")},
        {**SUCCESS, "url": CALLBACK + "#fragment"},
        {**SUCCESS, "url": CALLBACK + "&sessdata=other"},
        {**SUCCESS, "url": CALLBACK.replace("session%2Cvalue", "")},
        {**SUCCESS, "url": CALLBACK.replace("session%2Cvalue", "bad%0D%0Aheader")},
        {**SUCCESS, "url": CALLBACK.replace("session%2Cvalue", "bad%3Bcookie")},
        {**SUCCESS, "url": CALLBACK.replace("session%2Cvalue", "bad%ZZ")},
        {**SUCCESS, "url": CALLBACK.replace("session%2Cvalue", "%FF")},
        {**SUCCESS, "url": CALLBACK.replace("DedeUserID=123", "DedeUserID=-1")},
        {**SUCCESS, "url": CALLBACK.replace("a" * 32, "missing")},
        {**SUCCESS, "refresh_token": "bad;injected=value"},
    ],
)
async def test_invalid_completion_never_exposes_candidate(event: dict[str, object]) -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "passport.bilibili.com"
        return httpx.Response(200, json={"code": 0, "data": QR if request.url.path.endswith("generate") else event})

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        login = WebQrLogin(client)
        await login.generate_qrcode()
        with pytest.raises(QrLoginError):
            await login.check_state()
        with pytest.raises(QrLoginError):
            login.get_credential()


@pytest.mark.parametrize(
    "account", [{"isLogin": False, "mid": 123}, {"isLogin": True, "mid": 456}, {"isLogin": True, "mid": True}]
)
async def test_account_validation_rejects_invalid_or_mismatched_uid(account: dict[str, object]) -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        data = QR if request.url.path.endswith("generate") else SUCCESS
        if request.url.path.endswith("/nav"):
            assert request.url.host == "api.bilibili.com"
            assert "session%2Cvalue" in request.headers["cookie"]
            data = account
        else:
            assert "cookie" not in request.headers
        return httpx.Response(200, json={"code": 0, "data": data})

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        login = WebQrLogin(client)
        await login.generate_qrcode()
        assert await login.check_state() == QrState.VERIFY
        with pytest.raises(QrLoginError):
            await login.validate_credential()
        with pytest.raises(QrLoginError):
            login.get_credential()


@pytest.mark.parametrize("kind", ["large", "redirect", "foreign_qr"])
async def test_protocol_bounds_and_redirects(kind: str) -> None:
    seen: list[str] = []

    def reply(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if kind == "large":
            return httpx.Response(200, content=b" " * 65537)
        if kind == "redirect":
            return httpx.Response(302, headers={"Location": "https://evil.test/"})
        return httpx.Response(200, json={"code": 0, "data": {**QR, "url": "https://evil.test/scan"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply), follow_redirects=True) as client:
        with pytest.raises(QrLoginError):
            await WebQrLogin(client).generate_qrcode()
    assert seen == ["passport.bilibili.com"]


def install_protocol(monkeypatch: pytest.MonkeyPatch, nav: Callable[[], None] | None = None) -> None:
    """在 HTTP 边界替换平台服务，保留真实协议解析和任务状态机。"""
    from app.web import qr_login

    def reply(request: httpx.Request) -> httpx.Response:
        data = QR if request.url.path.endswith("generate") else SUCCESS
        if request.url.path.endswith("/nav"):
            if nav is not None:
                nav()
            data = {"isLogin": True, "mid": 123}
        return httpx.Response(200, json={"code": 0, "data": data})

    client_type = httpx.AsyncClient

    def client(**kwargs: object) -> httpx.AsyncClient:
        return client_type(transport=httpx.MockTransport(reply), **kwargs)

    monkeypatch.setattr(qr_login.httpx, "AsyncClient", client)


async def test_revoked_owner_after_account_verification_cannot_save(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.web import login_handler

    valid = True
    saved: list[str] = []

    def revoke() -> None:
        nonlocal valid
        valid = False

    async def authorized() -> bool:
        return valid

    install_protocol(monkeypatch, revoke)
    monkeypatch.setattr(login_handler, "_save_cookie", saved.append)
    manager = QrLoginManager()
    task = manager.tasks[manager.start("owner", authorize=authorized)]
    assert task.worker is not None
    await task.worker
    assert task.status == "error" and not task.image and not saved
    assert "管理员登录已失效" in task.error
    await manager.stop()


@pytest.mark.parametrize("operation", ["cancel", "stop"])
async def test_commit_is_awaited_before_cancel_or_shutdown(operation: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.web import login_handler

    install_protocol(monkeypatch)
    entered = threading.Event()
    release = threading.Event()
    saved: list[str] = []

    def save(cookie: str) -> None:
        entered.set()
        if not release.wait(5):
            raise RuntimeError("test commit was never released")
        saved.append(cookie)

    monkeypatch.setattr(login_handler, "_save_cookie", save)
    manager = QrLoginManager()
    token = manager.start("owner")
    task = manager.tasks[token]
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        assert task.status == "committing"
        pending = asyncio.create_task(manager.cancel(token, "owner") if operation == "cancel" else manager.stop())
        await asyncio.sleep(0.02)
        assert not pending.done() and not saved
        release.set()
        await asyncio.wait_for(pending, 3)
        assert task.status == "done" and len(saved) == 1 and not task.image
    finally:
        release.set()
        await manager.stop()


async def test_failed_transaction_does_not_claim_success(temp_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import settings_store
    from app.web import login_handler

    install_protocol(monkeypatch)
    settings_store.set_setting("bilibili_cookie", "previous-cookie")

    def fail(cookie: str) -> None:
        from app.db.entities import AppSetting
        from app.db.session import get_session

        with get_session() as db:
            row = db.get(AppSetting, "bilibili_cookie")
            assert row is not None
            row.value = cookie
            db.add(row)
            db.flush()
            raise OSError("simulated database failure with private-cookie")

    monkeypatch.setattr(login_handler, "_save_cookie", fail)
    manager = QrLoginManager()
    task = manager.tasks[manager.start("owner")]
    assert task.worker is not None
    await task.worker
    assert task.status == "error" and not task.image
    assert "private-cookie" not in task.error
    assert settings_store.get_setting("bilibili_cookie") == "previous-cookie"
    await manager.stop()


async def test_cancel_before_validation_does_not_write(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.web import login_handler, qr_login

    waiting = asyncio.Event()
    saved: list[str] = []

    async def reply(request: httpx.Request) -> httpx.Response:
        data = QR if request.url.path.endswith("generate") else SUCCESS
        if request.url.path.endswith("/nav"):
            waiting.set()
            await asyncio.Event().wait()
        return httpx.Response(200, json={"code": 0, "data": data})

    client_type = httpx.AsyncClient

    def client(**kwargs: object) -> httpx.AsyncClient:
        return client_type(transport=httpx.MockTransport(reply), **kwargs)

    monkeypatch.setattr(qr_login.httpx, "AsyncClient", client)
    monkeypatch.setattr(login_handler, "_save_cookie", saved.append)
    manager = QrLoginManager()
    token = manager.start("owner")
    try:
        await asyncio.wait_for(waiting.wait(), 3)
        with pytest.raises(QrLoginError):
            manager.start("other-owner")
        assert await manager.cancel(token, "owner")
        assert manager.tasks[token].status == "cancelled" and not saved
    finally:
        await manager.stop()
