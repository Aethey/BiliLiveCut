"""Docker 首次设置、会话撤销、API 兼容及浏览器权限边界回归。"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from app.core.docker_auth import COOKIE_NAME, AuthError, AuthStore

PASSWORD = "我的后台管理口令-123456"
ORIGIN = "http://127.0.0.1"


@pytest.fixture
def auth_store(tmp_path: Path) -> AuthStore:
    store = AuthStore(tmp_path, idle_seconds=60, session_seconds=300)
    code = store.boot("")
    assert code is not None
    store.initialize(code, PASSWORD, "setup")
    return store


@pytest.fixture
async def browser(
    temp_db: None, auth_store: AuthStore, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    from app.web import main

    monkeypatch.setattr(main, "_DOCKER_AUTH", auth_store)
    monkeypatch.setattr(main, "_REVIEWER_PASSWORDS", {"editor": "reviewer-password"})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app),
        base_url=ORIGIN,
        headers={"Origin": ORIGIN},
    ) as client:
        yield client


async def login(browser: httpx.AsyncClient, username: str = "admin", password: str = PASSWORD) -> dict[str, object]:
    response = await browser.post("/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    state = (await browser.get("/auth/status")).json()
    browser.headers["X-BLC-CSRF"] = state["csrf_token"]
    return state


async def test_first_visit_redirects_but_api_uses_401_without_basic_popup(browser: httpx.AsyncClient) -> None:
    page = await browser.get("/imports?room=1", headers={"Accept": "text/html"})
    assert page.status_code == 303
    assert page.headers["location"] == "/auth/login?next=%2Fimports%3Froom%3D1"
    api = await browser.get("/api/dashboard")
    assert api.status_code == 401 and api.headers["X-BLC-Auth-Required"] == "1"
    assert "www-authenticate" not in api.headers
    assert (await browser.get("/auth/login")).status_code == 200
    assert (await browser.get("/static/js/admin-session.js")).status_code == 200


async def test_login_cookie_and_state_do_not_leak_password(browser: httpx.AsyncClient) -> None:
    response = await browser.post("/auth/login", json={"username": "admin", "password": PASSWORD})
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/" in cookie
    assert PASSWORD not in response.text and PASSWORD not in cookie
    state = (await browser.get("/auth/status")).json()
    assert state["authenticated"] and state["role"] == "admin"
    assert "verifier" not in state and "token" not in state
    assert (await browser.get("/api/dashboard")).status_code == 200
    assert (await browser.get("/")).headers["cache-control"] == "no-store"


async def test_https_session_cookie_is_secure(browser: httpx.AsyncClient) -> None:
    response = await browser.post(
        "https://127.0.0.1/auth/login",
        headers={"Origin": "https://127.0.0.1"},
        json={"username": "admin", "password": PASSWORD},
    )
    assert response.status_code == 200 and "Secure" in response.headers["set-cookie"]


@pytest.mark.parametrize("path", ["/auth/login", "/auth/setup", "/auth/password", "/auth/logout"])
@pytest.mark.parametrize("origin", ["https://attacker.example", "", "null"])
async def test_auth_mutations_require_same_origin(browser: httpx.AsyncClient, path: str, origin: str) -> None:
    result = await browser.post(path, headers={"Origin": origin}, json={"username": "admin", "password": PASSWORD})
    assert result.status_code == 403 and not result.cookies


async def test_cookie_writes_require_csrf_and_do_not_execute_on_failure(browser: httpx.AsyncClient) -> None:
    await login(browser)
    csrf = browser.headers.pop("X-BLC-CSRF")
    denied = await browser.post("/auth/logout", json={})
    assert denied.status_code == 403
    assert (await browser.get("/auth/status")).json()["authenticated"]
    api = await browser.post("/api/rooms", json={})
    assert api.status_code == 403
    browser.headers["X-BLC-CSRF"] = csrf
    assert (await browser.post("/auth/logout", json={})).status_code == 200
    assert not (await browser.get("/auth/status")).json()["authenticated"]


async def test_basic_api_remains_supported_and_roles_are_enforced(browser: httpx.AsyncClient) -> None:
    header = "Basic " + base64.b64encode(f"admin:{PASSWORD}".encode()).decode()
    assert (await browser.get("/api/dashboard", headers={"Authorization": header})).status_code == 200
    assert not browser.cookies
    reviewer = "Basic " + base64.b64encode(b"editor:reviewer-password").decode()
    assert (await browser.get("/review/queue", headers={"Authorization": reviewer})).status_code == 200
    assert (await browser.get("/api/settings", headers={"Authorization": reviewer})).status_code == 403
    await login(browser, "editor", "reviewer-password")
    assert (await browser.get("/review/queue")).status_code == 200
    assert (await browser.get("/api/settings")).status_code == 403
    assert (
        await browser.post(
            "/auth/password", json={"current_password": "reviewer-password", "password": "replacement-secret"}
        )
    ).status_code == 403


async def test_logout_revokes_token_even_if_stolen_cookie_is_replayed(browser: httpx.AsyncClient) -> None:
    await login(browser)
    cookie = browser.cookies.get(COOKIE_NAME)
    assert (await browser.post("/auth/logout", json={})).status_code == 200
    browser.cookies.set(COOKIE_NAME, cookie)
    assert (await browser.get("/api/dashboard")).status_code == 401


async def test_login_rotates_current_session(browser: httpx.AsyncClient, auth_store: AuthStore) -> None:
    await login(browser)
    old = browser.cookies.get(COOKIE_NAME)
    await login(browser)
    assert browser.cookies.get(COOKIE_NAME) != old
    assert auth_store.session(old) is None


async def test_logout_all_revokes_other_devices(browser: httpx.AsyncClient, auth_store: AuthStore) -> None:
    other = auth_store.authenticate("admin", PASSWORD, {}, "other", create_session=True)["token"]
    await login(browser)
    assert (await browser.post("/auth/logout", json={"everywhere": True})).status_code == 200
    assert auth_store.session(other) is None


async def test_password_change_revokes_sessions_and_basic_old_password(
    browser: httpx.AsyncClient, auth_store: AuthStore
) -> None:
    await login(browser)
    old = browser.cookies.get(COOKIE_NAME)
    other = auth_store.authenticate("admin", PASSWORD, {}, "other", create_session=True)["token"]
    response = await browser.post(
        "/auth/password", json={"current_password": PASSWORD, "password": "replacement-secret"}
    )
    assert response.status_code == 200
    assert auth_store.session(old) is None and auth_store.session(other) is None
    assert auth_store.authenticate("admin", PASSWORD, {}, "other") is None
    assert auth_store.authenticate("admin", "replacement-secret", {}, "other")
    assert PASSWORD.encode() not in auth_store.path.read_bytes()
    await login(browser, password="replacement-secret")


async def test_environment_password_cannot_be_overwritten_in_web(
    browser: httpx.AsyncClient, auth_store: AuthStore
) -> None:
    auth_store.boot(PASSWORD)
    await login(browser)
    result = await browser.post("/auth/password", json={"current_password": PASSWORD, "password": "replacement-secret"})
    assert result.status_code == 409
    assert auth_store.authenticate("admin", PASSWORD, {}, "local")
    with pytest.raises(AuthError, match="ADMIN_PASSWORD"):
        auth_store.reset()


async def test_wrong_password_rate_limit_survives_restart(browser: httpx.AsyncClient, auth_store: AuthStore) -> None:
    for _ in range(5):
        result = await browser.post("/auth/login", json={"username": "admin", "password": "wrong"})
        assert result.status_code == 401
    auth_store.boot("")
    result = await browser.post("/auth/login", json={"username": "admin", "password": PASSWORD})
    assert result.status_code == 429
    with patch("app.core.docker_auth.time.time", return_value=4_000_000_000):
        assert (await browser.post("/auth/login", json={"username": "admin", "password": PASSWORD})).status_code == 200


async def test_missing_auth_storage_fails_closed(browser: httpx.AsyncClient, auth_store: AuthStore) -> None:
    await login(browser)
    auth_store.path.unlink()
    assert (await browser.get("/api/dashboard")).status_code == 503
    assert (await browser.post("/auth/login", json={"username": "admin", "password": PASSWORD})).status_code == 503
    assert not auth_store.path.exists()


async def test_expired_session_requires_relogin_and_preserves_server_data(browser: httpx.AsyncClient) -> None:
    with patch("app.core.docker_auth.time.time", return_value=1000):
        await login(browser)
    with patch("app.core.docker_auth.time.time", return_value=1061):
        result = await browser.get("/api/dashboard")
        assert result.status_code == 401 and result.headers["X-BLC-Auth-Required"] == "1"
        await login(browser)
        assert (await browser.get("/api/dashboard")).status_code == 200


async def test_setup_code_is_required_consumed_and_never_an_api_password(
    browser: httpx.AsyncClient, auth_store: AuthStore
) -> None:
    code = auth_store.reset()
    result = await browser.post("/auth/login", json={"username": "admin", "password": code})
    assert result.status_code == 401
    wrong = await browser.post("/auth/setup", json={"code": "wrong", "password": PASSWORD})
    assert wrong.status_code == 401
    assert (await browser.get("/api/dashboard")).status_code == 401
    assert (await browser.post("/auth/setup", json={"code": code, "password": PASSWORD})).status_code == 200
    assert (await browser.post("/auth/setup", json={"code": code, "password": "other-password"})).status_code == 409
    await login(browser)
    state = (await browser.get("/auth/status")).json()
    assert not state["setup_required"] and code not in str(state)
    assert code.encode() not in auth_store.path.read_bytes()


@pytest.mark.parametrize("payload", ["[]", "{", '{"password": ["secret"]}', '{"password": "' + "x" * 5000 + '"}'])
async def test_malformed_or_large_input_does_not_echo_credentials(browser: httpx.AsyncClient, payload: str) -> None:
    result = await browser.post("/auth/login", content=payload, headers={"Content-Type": "application/json"})
    assert result.status_code in (400, 413)
    assert "secret" not in result.text


def test_absolute_expiry_applies_despite_activity(auth_store: AuthStore) -> None:
    with patch("app.core.docker_auth.time.time", return_value=1000):
        token = auth_store.authenticate("admin", PASSWORD, {}, "local", create_session=True)["token"]
    for now in range(1050, 1300, 50):
        with patch("app.core.docker_auth.time.time", return_value=now):
            assert auth_store.session(token)
    with patch("app.core.docker_auth.time.time", return_value=1300):
        assert auth_store.session(token) is None


def test_status_poll_does_not_extend_idle_timeout(auth_store: AuthStore) -> None:
    with patch("app.core.docker_auth.time.time", return_value=1000):
        token = auth_store.authenticate("admin", PASSWORD, {}, "local", create_session=True)["token"]
    with patch("app.core.docker_auth.time.time", return_value=1059):
        assert auth_store.session(token, touch=False)
    with patch("app.core.docker_auth.time.time", return_value=1060):
        assert auth_store.session(token) is None


def test_reset_invalidates_password_sessions_and_previous_setup_code(auth_store: AuthStore) -> None:
    token = auth_store.authenticate("admin", PASSWORD, {}, "local", create_session=True)["token"]
    first = auth_store.reset()
    second = auth_store.reset()
    assert auth_store.session(token) is None and first != second
    with pytest.raises(AuthError):
        auth_store.initialize(first, PASSWORD, "local")
    auth_store.initialize(second, PASSWORD, "local")


def test_setup_code_expires(tmp_path: Path) -> None:
    store = AuthStore(tmp_path)
    with patch("app.core.docker_auth.time.time", return_value=1000):
        code = store.boot("")
    with patch("app.core.docker_auth.time.time", return_value=2800):
        with pytest.raises(AuthError, match="过期"):
            store.initialize(code, PASSWORD, "local")
        assert store.state()["setup_required"]


@pytest.mark.parametrize("password", ["short", " " * 20, "x" * 129, "long-password\n"])
def test_invalid_new_password_does_not_consume_setup_code(tmp_path: Path, password: str) -> None:
    store = AuthStore(tmp_path)
    code = store.boot("")
    with pytest.raises(AuthError, match="12–128"):
        store.initialize(code, password, "local")
    store.initialize(code, PASSWORD, "local")


def test_restart_preserves_hash_password_and_revokes_session(auth_store: AuthStore) -> None:
    token = auth_store.authenticate("admin", PASSWORD, {}, "local", create_session=True)["token"]
    assert auth_store.boot("") is None
    assert auth_store.authenticate("admin", PASSWORD, {}, "local")
    assert auth_store.session(token) is None


def test_removing_environment_password_requires_fresh_initialization(auth_store: AuthStore) -> None:
    auth_store.boot("deployment-password")
    assert auth_store.boot("") is not None
    with pytest.raises(AuthError, match="初始化"):
        auth_store.authenticate("admin", PASSWORD, {}, "local")


def test_session_tokens_are_hashed_and_account_session_count_is_bounded(auth_store: AuthStore) -> None:
    # 真实事务与会话签发；只在密码哈希边界替换耗时比较。
    with patch("app.core.docker_auth.password_matches", return_value=True):
        tokens = [
            auth_store.authenticate("admin", PASSWORD, {}, "local", create_session=True)["token"] for _ in range(12)
        ]
    assert auth_store.session(tokens[0]) is None
    assert auth_store.session(tokens[-1])
    assert tokens[-1].encode() not in auth_store.path.read_bytes()
    with auth_store.connect() as db:
        assert db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 10


def test_setup_race_has_exactly_one_winner(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    store = AuthStore(tmp_path)
    code = store.boot("")

    def initialize(index: int) -> bool:
        try:
            store.initialize(code, PASSWORD + str(index), str(index))
            return True
        except AuthError as exc:
            assert exc.status == 409
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sum(executor.map(initialize, (1, 2))) == 1
    assert not store.state()["setup_required"]


def test_shared_permissions_and_symlink_auth_storage_are_rejected(auth_store: AuthStore) -> None:
    import os
    import stat
    from types import SimpleNamespace

    original = Path.lstat

    def unsafe(path: Path) -> object:
        metadata = original(path)
        if path == auth_store.path:
            return SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_uid=metadata.st_uid)
        return metadata

    uid = auth_store.path.stat().st_uid
    with (
        patch.object(os, "name", "posix"),
        patch.object(os, "getuid", return_value=uid, create=True),
        patch.object(Path, "lstat", unsafe),
    ):
        with pytest.raises(RuntimeError, match="600"):
            auth_store.state()

    def link(path: Path) -> object:
        return SimpleNamespace(st_mode=stat.S_IFLNK) if path == auth_store.path else original(path)

    with patch.object(Path, "lstat", link):
        with pytest.raises(RuntimeError, match="符号链接"):
            auth_store.state()


async def test_stale_csrf_after_another_tab_login_has_recoverable_response(browser: httpx.AsyncClient) -> None:
    state = await login(browser)
    old = state["csrf_token"]
    await login(browser)
    browser.headers["X-BLC-CSRF"] = old
    result = await browser.post("/api/rooms", json={})
    assert result.status_code == 403 and result.headers["X-BLC-CSRF-Required"] == "1"
    assert (await browser.get("/auth/status")).json()["csrf_token"] != old


async def test_incorrect_current_password_keeps_existing_session(browser: httpx.AsyncClient) -> None:
    await login(browser)
    result = await browser.post("/auth/password", json={"current_password": "wrong", "password": "replacement-secret"})
    assert result.status_code == 403
    assert (await browser.get("/api/dashboard")).status_code == 200


async def test_reset_does_not_allow_old_session_to_access_management(
    browser: httpx.AsyncClient, auth_store: AuthStore
) -> None:
    await login(browser)
    auth_store.reset()
    assert (await browser.get("/api/dashboard")).status_code == 401


def test_bounded_failure_table_does_not_evict_active_lockouts(auth_store: AuthStore) -> None:
    import time

    with auth_store.connect() as db:
        db.executemany("INSERT INTO failures VALUES (?, 5, ?)", [(str(i), time.time()) for i in range(1000)])
    with pytest.raises(AuthError) as error:
        auth_store.authenticate("admin", PASSWORD, {}, "new-source")
    assert error.value.status == 429
    with auth_store.connect() as db:
        assert db.execute("SELECT count(*) FROM failures").fetchone()[0] == 1000


async def test_explicit_authorization_cannot_fall_back_to_admin_cookie(browser: httpx.AsyncClient) -> None:
    await login(browser)
    assert (await browser.get("/api/dashboard", headers={"Authorization": "Basic invalid"})).status_code == 400


@pytest.mark.parametrize("contents", [b"", b"not-a-sqlite-file"])
def test_existing_invalid_store_is_never_overwritten(tmp_path: Path, contents: bytes) -> None:
    import sqlite3

    store = AuthStore(tmp_path)
    store.path.parent.mkdir()
    store.path.write_bytes(contents)
    store.path.chmod(0o600)
    with pytest.raises((RuntimeError, sqlite3.DatabaseError)):
        store.boot("")
    assert store.path.read_bytes() == contents


@pytest.mark.parametrize("damage", ["mode", "verifier", "missing"])
def test_damaged_credentials_fail_closed_without_reinitialization(auth_store: AuthStore, damage: str) -> None:
    with auth_store.connect() as db:
        if damage == "missing":
            db.execute("DELETE FROM identity")
        else:
            db.execute(f"UPDATE identity SET {damage}='damaged'")
    with pytest.raises(RuntimeError, match="损坏"):
        auth_store.boot("")
    with pytest.raises(RuntimeError, match="损坏"):
        auth_store.state()


async def test_json_surrogate_is_rejected_without_leaking_input(browser: httpx.AsyncClient) -> None:
    result = await browser.post(
        "/auth/login", content='{"username":"admin","password":"\\ud800"}', headers={"Content-Type": "application/json"}
    )
    assert result.status_code == 400
    assert "ud800" not in result.text


async def test_browser_cached_basic_cannot_bypass_logout(browser: httpx.AsyncClient) -> None:
    await login(browser)
    header = "Basic " + base64.b64encode(f"admin:{PASSWORD}".encode()).decode()
    browser.headers.update({"Authorization": header, "Sec-Fetch-Site": "same-origin"})
    assert (await browser.get("/api/dashboard")).status_code == 200
    assert (await browser.post("/auth/logout", json={})).status_code == 200
    result = await browser.get("/api/dashboard")
    assert result.status_code == 401 and result.headers["X-BLC-Auth-Required"] == "1"


async def test_browser_stale_basic_does_not_override_valid_session(browser: httpx.AsyncClient) -> None:
    await login(browser)
    header = "Basic " + base64.b64encode(b"admin:obsolete-password").decode()
    result = await browser.get("/api/dashboard", headers={"Authorization": header, "Sec-Fetch-Site": "same-origin"})
    assert result.status_code == 200
