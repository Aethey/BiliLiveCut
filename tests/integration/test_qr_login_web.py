"""扫码路由、Cookie 持久化和权限边界验收。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.web.qr_login import LoginTask, QrLoginManager


@pytest.mark.parametrize("header_credentials", [False, True])
async def test_headless_qr_confirm_persists_cookie_without_returning_it(
    temp_db: None, monkeypatch: pytest.MonkeyPatch, header_credentials: bool
) -> None:
    from app.core import settings_store
    from app.web import main
    from app.web import qr_login as service
    from app.web.routers import auth

    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    events = asyncio.Queue()

    async def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/nav"):
            return httpx.Response(200, json={"code": 0, "data": {"isLogin": True, "mid": 123}})
        if request.url.path.endswith("generate"):
            data = {"url": "https://passport.bilibili.com/scan?qrcode_key=test-key", "qrcode_key": "test-key"}
        else:
            data = await events.get()
        headers = []
        if header_credentials and data.get("code") == 0:
            data = {**data, "url": "https://passport.biligame.com/crossDomain?gourl=https%3A%2F%2Fwww.bilibili.com"}
            headers = [
                ("Set-Cookie", f"{key}={value}; Domain=.bilibili.com; Path=/; Secure")
                for key, value in {
                    "SESSDATA": "session%2Cencoded",
                    "bili_jct": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "DedeUserID": "123",
                }.items()
            ]
        return httpx.Response(200, json={"code": 0, "data": data}, headers=headers)

    original_client = httpx.AsyncClient

    def isolated_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs.setdefault("transport", httpx.MockTransport(respond))
        return original_client(*args, **kwargs)

    monkeypatch.setattr(service.httpx, "AsyncClient", isolated_client)
    manager = QrLoginManager()
    manager.poll_interval = 0
    monkeypatch.setattr(auth, "qr_logins", manager)
    monkeypatch.setattr(service, "qr_logins", manager)
    settings_store.set_setting("bilibili_cookie", "old-cookie")

    async def wait_state(token: str, state: str) -> None:
        async def wait() -> None:
            while manager.tasks[token].status != state:
                await asyncio.sleep(0.005)

        await asyncio.wait_for(wait(), timeout=3)

    try:
        async with original_client(transport=httpx.ASGITransport(app=main.app), base_url="http://127.0.0.1") as client:
            token = (await client.post("/api/login/qrcode")).json()["task_id"]
            await events.put({"code": 86101})
            await wait_state(token, "scan")
            state = (await client.get(f"/api/login/qrcode/{token}")).json()
            assert state["qrcode"].startswith("data:image/svg+xml;base64,")
            assert settings_store.get_setting("bilibili_cookie") == "old-cookie"
            await events.put({"code": 86090})
            await wait_state(token, "confirm")
            assert settings_store.get_setting("bilibili_cookie") == "old-cookie"
            await events.put(
                {
                    "code": 0,
                    "url": "https://passport.bilibili.com/callback?SESSDATA=session%2Cencoded&bili_jct=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa&DedeUserID=123",
                    "refresh_token": "private-refresh",
                }
            )
            await wait_state(token, "done")
            response = await client.get(f"/api/login/qrcode/{token}")
            assert response.json()["status"] == "done" and not response.json()["qrcode"]
            assert "session%2Cencoded" not in response.text and "private-refresh" not in response.text
            assert "SESSDATA=session%2Cencoded" in settings_store.get_setting("bilibili_cookie")
            assert "ac_time_value=private-refresh" in settings_store.get_setting("bilibili_cookie")
            assert (await client.get("/api/cookie-status")).json() == {"has_cookie": True, "uid": "123"}
            assert (await client.post(f"/api/login/qrcode/{token}/cancel")).json()["status"] == "done"
        await manager.stop()
        assert "SESSDATA=session%2Cencoded" in settings_store.get_setting("bilibili_cookie")
    finally:
        await manager.stop()


def test_qr_routes_and_shutdown_without_browser(temp_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.web import main
    from app.web.main import app
    from app.web.routers import auth

    async def run(self: QrLoginManager, task: LoginTask) -> None:
        task.status = "scan"
        task.image = "data:image/svg+xml;base64,PHN2Zy8+"
        await asyncio.Event().wait()

    manager = QrLoginManager()
    monkeypatch.setattr(QrLoginManager, "_run", run)
    monkeypatch.setattr(auth, "qr_logins", manager)
    import app.web.qr_login as service

    monkeypatch.setattr(service, "qr_logins", manager)
    with TestClient(app) as client:
        response = client.post("/api/login/qrcode")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        token = response.json()["task_id"]
        assert client.post("/api/login/qrcode").status_code == 409
        restored = client.get("/api/login/qrcode")
        assert restored.json()["task_id"] == token
        assert restored.headers["cache-control"] == "no-store"
        assert 0 < restored.json()["expires_in"] <= 600
        state = client.get(f"/api/login/qrcode/{token}")
        assert state.json()["status"] == "scan"
        assert state.json()["qrcode"].startswith("data:image/svg+xml;base64,")
        assert "cookie" not in state.json()
        assert client.post(f"/api/login/qrcode/{token}/cancel").status_code == 200
        assert client.get(f"/api/login/qrcode/{token}").json()["status"] == "cancelled"
        assert client.get("/api/login/qrcode/unknown").status_code == 404
        assert client.post("/api/login/qrcode", headers={"Origin": "https://evil.example"}).status_code == 403
        monkeypatch.setattr(main, "_ADMIN_PASSWORD", "test-password")
        assert client.get(f"/api/login/qrcode/{token}").status_code == 401
    assert not manager.tasks


def test_qr_task_is_bound_to_validated_browser_session(
    temp_db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    from app.core.docker_auth import AuthStore
    from app.web import main
    from app.web import qr_login as service
    from app.web.main import app
    from app.web.routers import auth

    async def run(self: QrLoginManager, task: LoginTask) -> None:
        task.status = "scan"
        await asyncio.Event().wait()

    manager = QrLoginManager()
    monkeypatch.setattr(QrLoginManager, "_run", run)
    monkeypatch.setattr(auth, "qr_logins", manager)
    monkeypatch.setattr(service, "qr_logins", manager)
    store = AuthStore(tmp_path)
    store.boot("test-password-long")
    monkeypatch.setattr(main, "_DOCKER_AUTH", store)

    with TestClient(app) as client:
        client.post(
            "/auth/login",
            json={"username": "admin", "password": "test-password-long"},
            headers={"Origin": "http://testserver"},
        )
        csrf = client.get("/auth/status").json()["csrf_token"]
        response = client.post("/api/login/qrcode", headers={"X-BLC-CSRF": csrf})
        assert response.status_code == 200
        token = response.json()["task_id"]
        first_cookie = httpx.Cookies(client.cookies)
        assert client.get(f"/api/login/qrcode/{token}").status_code == 200
        client.cookies.clear()
        client.post(
            "/auth/login",
            json={"username": "admin", "password": "test-password-long"},
            headers={"Origin": "http://testserver"},
        )
        assert client.get(f"/api/login/qrcode/{token}").status_code == 404
        client.cookies = first_cookie
        assert client.get(f"/api/login/qrcode/{token}").status_code == 200


@pytest.mark.parametrize("mode", ["local", "basic"])
def test_same_username_different_browsers_cannot_share_qr(
    mode: str, temp_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.web import main, qr_login
    from app.web.routers import auth

    async def run(self: QrLoginManager, task: LoginTask) -> None:
        task.status = "scan"
        await asyncio.Event().wait()

    manager = QrLoginManager()
    monkeypatch.setattr(QrLoginManager, "_run", run)
    monkeypatch.setattr(auth, "qr_logins", manager)
    monkeypatch.setattr(qr_login, "qr_logins", manager)
    monkeypatch.setattr(main, "_DOCKER_AUTH", None)
    monkeypatch.setattr(main, "_ADMIN_PASSWORD", "fixture-password" if mode == "basic" else "")
    with TestClient(main.app, base_url="https://testserver") as first:
        credentials = ("admin", "fixture-password") if mode == "basic" else None
        first.auth = credentials
        start = first.post("/api/login/qrcode")
        assert start.status_code == 200
        token = start.json()["task_id"]
        header = start.headers["set-cookie"]
        assert "HttpOnly" in header and "Secure" in header and "SameSite=strict" in header
        assert first.get(f"/api/login/qrcode/{token}").status_code == 200
        original = httpx.Cookies(first.cookies)
        first.cookies.clear()
        assert first.get(f"/api/login/qrcode/{token}").status_code == 404
        assert first.post(f"/api/login/qrcode/{token}/cancel").status_code == 404
        assert first.post("/api/login/qrcode").status_code == 409
        assert first.get("/api/login/qrcode").json() == {"task_id": None, "status": "idle"}
        first.cookies.set("blc_qr_browser", "forged.9999999999.signature", path="/api/login")
        assert first.get(f"/api/login/qrcode/{token}").status_code == 404
        first.cookies = original
        assert first.post(f"/api/login/qrcode/{token}/cancel").json()["status"] == "cancelled"


async def test_docker_logout_during_account_validation_preserves_old_cookie(
    temp_db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core import settings_store
    from app.core.docker_auth import AuthStore
    from app.web import main, qr_login
    from app.web.routers import auth

    validating = asyncio.Event()
    release = asyncio.Event()

    async def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("generate"):
            data = {
                "url": "https://account.bilibili.com/h5/account-h5/auth/scan-web?k=fixture",
                "qrcode_key": "fixture",
            }
        elif request.url.path.endswith("/nav"):
            validating.set()
            await release.wait()
            data = {"isLogin": True, "mid": 123}
        else:
            data = {
                "code": 0,
                "url": "https://passport.bilibili.com/callback?SESSDATA=fixture&bili_jct="
                + "a" * 32
                + "&DedeUserID=123",
            }
        return httpx.Response(200, json={"code": 0, "data": data})

    original_client = httpx.AsyncClient

    def isolated_client(**kwargs: object) -> httpx.AsyncClient:
        return original_client(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(qr_login.httpx, "AsyncClient", isolated_client)
    manager = QrLoginManager()
    monkeypatch.setattr(auth, "qr_logins", manager)
    store = AuthStore(tmp_path / "auth")
    store.boot("test-password-long")
    monkeypatch.setattr(main, "_DOCKER_AUTH", store)
    settings_store.set_setting("bilibili_cookie", "old-cookie")
    try:
        async with original_client(transport=httpx.ASGITransport(app=main.app), base_url="http://127.0.0.1") as client:
            response = await client.post(
                "/auth/login",
                json={"username": "admin", "password": "test-password-long"},
                headers={"Origin": "http://127.0.0.1"},
            )
            assert response.status_code == 200
            csrf = (await client.get("/auth/status")).json()["csrf_token"]
            headers = {"Origin": "http://127.0.0.1", "X-BLC-CSRF": csrf}
            response = await client.post("/api/login/qrcode", headers=headers)
            assert response.status_code == 200
            task = manager.tasks[response.json()["task_id"]]
            await asyncio.wait_for(validating.wait(), 3)
            response = await client.post("/auth/logout", json={}, headers=headers)
            assert response.status_code == 200
            release.set()
            assert task.worker is not None
            await asyncio.wait_for(task.worker, 3)
            assert task.status == "error" and not task.image
            assert settings_store.get_setting("bilibili_cookie") == "old-cookie"
    finally:
        release.set()
        await manager.stop()


def test_qr_browser_renewal_preserves_task_identity(temp_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from app.web import main, qr_login
    from app.web.routers import auth

    async def run(self: QrLoginManager, task: LoginTask) -> None:
        task.status = "scan"
        await asyncio.Event().wait()

    manager = QrLoginManager()
    monkeypatch.setattr(QrLoginManager, "_run", run)
    monkeypatch.setattr(auth, "qr_logins", manager)
    monkeypatch.setattr(qr_login, "qr_logins", manager)
    monkeypatch.setattr(main, "_DOCKER_AUTH", None)
    monkeypatch.setattr(main, "_ADMIN_PASSWORD", "")
    now = auth._login_time.time()
    monkeypatch.setattr(auth, "_login_time", SimpleNamespace(time=lambda: now))
    with TestClient(main.app) as client:
        assert client.get("/api/login/qrcode").json()["task_id"] is None
        old_browser = client.cookies.get("blc_qr_browser")
        now += 1700
        response = client.post("/api/login/qrcode")
        assert response.status_code == 200
        task_id = response.json()["task_id"]
        renewed = client.cookies.get("blc_qr_browser")
        assert old_browser != renewed
        now += 200
        assert client.get(f"/api/login/qrcode/{task_id}").status_code == 200
        assert client.get("/api/login/qrcode").json()["task_id"] == task_id
