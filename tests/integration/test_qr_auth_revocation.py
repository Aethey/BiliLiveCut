"""Basic 扫码凭据撤销和最终写入事务边界回归。"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import httpx
import pytest

from app.core.docker_auth import AuthError, AuthStore
from app.web.qr_login import QrLoginManager

PASSWORD = "initial-fixture-password"


@pytest.mark.parametrize("revocation", ["none", "change", "same_password", "reset", "reinitialize"])
async def test_basic_qr_cannot_persist_after_credential_revocation(
    revocation: str, temp_db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core import settings_store
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
    manager.poll_interval = 0
    monkeypatch.setattr(auth, "qr_logins", manager)
    store = AuthStore(tmp_path / "auth")
    code = store.boot("")
    assert code is not None
    store.initialize(code, PASSWORD, "setup")
    monkeypatch.setattr(main, "_DOCKER_AUTH", store)
    settings_store.set_setting("bilibili_cookie", "old-cookie")
    identity = store.authenticate("admin", PASSWORD, {}, "local", create_session=True)
    assert identity is not None
    try:
        async with original_client(
            transport=httpx.ASGITransport(app=main.app), base_url="http://127.0.0.1", auth=("admin", PASSWORD)
        ) as client:
            response = await client.post("/api/login/qrcode")
            assert response.status_code == 200
            assert identity["credential_version"] not in response.text
            task = manager.tasks[response.json()["task_id"]]
            await asyncio.wait_for(validating.wait(), 3)
            if revocation in {"change", "same_password"}:
                replacement = PASSWORD if revocation == "same_password" else "replacement-fixture-password"
                await asyncio.to_thread(store.change_password, identity["token"], PASSWORD, replacement, "local")
            elif revocation in {"reset", "reinitialize"}:
                code = await asyncio.to_thread(store.reset)
                if revocation == "reinitialize":
                    await asyncio.to_thread(store.initialize, code, PASSWORD, "setup")
            expected_status = 401 if revocation in {"change", "reset"} else 200
            assert (await client.get("/api/cookie-status")).status_code == expected_status
            release.set()
            assert task.worker is not None
            await asyncio.wait_for(task.worker, 3)
            if revocation == "none":
                assert task.status == "done"
                assert "SESSDATA=fixture" in settings_store.get_setting("bilibili_cookie")
            else:
                assert task.status == "error"
                assert "管理员登录已失效" in task.error
                assert settings_store.get_setting("bilibili_cookie") == "old-cookie"
            assert not task.image and task.authorize is None and task.persist is None
    finally:
        release.set()
        await manager.stop()


def test_final_write_rechecks_revoked_credentials(tmp_path: Path) -> None:
    store = AuthStore(tmp_path)
    code = store.boot("")
    assert code is not None
    store.initialize(code, PASSWORD, "setup")
    authenticated = store.authenticate("admin", PASSWORD, {}, "local", create_session=True)
    assert authenticated is not None
    version = authenticated["credential_version"]
    assert store.admin_authorized(credential_version=version)
    store.reset()
    writes: list[str] = []
    for credentials in ({"credential_version": version}, {"session": authenticated["token"]}, {}):
        with pytest.raises(AuthError, match="管理员登录已失效"):
            store.run_admin_action(lambda: writes.append("unexpected"), **credentials)
    assert not writes


def test_started_write_finishes_before_password_revocation(tmp_path: Path) -> None:
    store = AuthStore(tmp_path)
    code = store.boot("")
    assert code is not None
    store.initialize(code, PASSWORD, "setup")
    authenticated = store.authenticate("admin", PASSWORD, {}, "local", create_session=True)
    assert authenticated is not None
    started, release, revoking, revoked = Event(), Event(), Event(), Event()
    order: list[str] = []

    def write() -> None:
        started.set()
        assert release.wait(3)
        order.append("write")

    def revoke() -> None:
        revoking.set()
        store.change_password(authenticated["token"], PASSWORD, "replacement-fixture-password", "local")
        order.append("revoked")
        revoked.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        saved = pool.submit(store.run_admin_action, write, credential_version=authenticated["credential_version"])
        try:
            assert started.wait(3)
            changed = pool.submit(revoke)
            assert revoking.wait(3)
            assert not revoked.wait(0.1)
        finally:
            release.set()
        saved.result(timeout=3)
        changed.result(timeout=3)
    assert order == ["write", "revoked"]
    assert not store.admin_authorized(credential_version=authenticated["credential_version"])


def test_reviewer_cannot_authorize_admin_write(tmp_path: Path) -> None:
    store = AuthStore(tmp_path)
    store.boot(PASSWORD)
    reviewer = store.authenticate(
        "reviewer", "reviewer-fixture", {"reviewer": "reviewer-fixture"}, "local", create_session=True
    )
    assert reviewer is not None and not reviewer["credential_version"]
    assert not store.admin_authorized(session=reviewer["token"])
    assert not store.admin_authorized(credential_version=reviewer["credential_version"])
