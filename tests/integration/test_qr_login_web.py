"""扫码路由、Cookie 持久化和权限边界验收。"""

from __future__ import annotations

import asyncio

import httpx
from fastapi.testclient import TestClient

from app.web.qr_login import QrLoginManager


async def test_headless_qr_confirm_persists_cookie_without_returning_it(temp_db, monkeypatch) -> None:
    from app.core import settings_store
    from app.web import main
    from app.web import qr_login as service
    from app.web.routers import auth

    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    events = asyncio.Queue()

    async def respond(request):
        if request.url.path.endswith("generate"):
            data = {"url": "https://passport.bilibili.com/scan?qrcode_key=test-key", "qrcode_key": "test-key"}
        else:
            data = await events.get()
        return httpx.Response(200, json={"code": 0, "data": data})

    original_client = httpx.AsyncClient

    def isolated_client(*args, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(respond))
        return original_client(*args, **kwargs)

    monkeypatch.setattr(service.httpx, "AsyncClient", isolated_client)
    manager = QrLoginManager()
    manager.poll_interval = 0
    monkeypatch.setattr(auth, "qr_logins", manager)
    monkeypatch.setattr(service, "qr_logins", manager)
    settings_store.set_setting("bilibili_cookie", "old-cookie")

    async def wait_state(token, state):
        async def wait():
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
                    "url": "https://passport.bilibili.com/callback?SESSDATA=session%2Cencoded&bili_jct=csrf&DedeUserID=123",
                    "refresh_token": "private-refresh",
                }
            )
            await wait_state(token, "done")
            response = await client.get(f"/api/login/qrcode/{token}")
            assert response.json()["status"] == "done" and not response.json()["qrcode"]
            assert "session%2Cencoded" not in response.text and "private-refresh" not in response.text
            assert "SESSDATA=session%2Cencoded" in settings_store.get_setting("bilibili_cookie")
            assert "ac_time_value=private-refresh" in settings_store.get_setting("bilibili_cookie")
            assert (await client.post(f"/api/login/qrcode/{token}/cancel")).json()["status"] == "done"
        await manager.stop()
        assert "SESSDATA=session%2Cencoded" in settings_store.get_setting("bilibili_cookie")
    finally:
        await manager.stop()


def test_qr_routes_and_shutdown_without_browser(temp_db, monkeypatch) -> None:
    from app.web import main
    from app.web.main import app
    from app.web.routers import auth

    async def run(self, task):
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


def test_qr_task_is_bound_to_validated_browser_session(temp_db, tmp_path, monkeypatch) -> None:
    import httpx

    from app.core.docker_auth import AuthStore
    from app.web import main
    from app.web import qr_login as service
    from app.web.main import app
    from app.web.routers import auth

    async def run(self, task):
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
        first_cookie = httpx.Cookies(client.cookies)
        response = client.post("/api/login/qrcode", headers={"X-BLC-CSRF": csrf})
        assert response.status_code == 200
        token = response.json()["task_id"]
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
