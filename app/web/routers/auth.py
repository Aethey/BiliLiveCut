"""登录管理."""

from __future__ import annotations

import asyncio
import hmac
import secrets
import time as _login_time
from hashlib import sha256
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.core.docker_auth import AuthError
from app.core.sanitize import safe_exception_summary
from app.sources.bilibili.login import QrLoginError
from app.web.login_handler import get_cookie_info, get_login_status, start_login
from app.web.qr_login import LoginTask, qr_logins

_LOGIN_FAILURES: dict[str, list[float]] = {}
_MAX_LOGIN_ATTEMPTS = 5
_LOGIN_WINDOW_S = 300


def _check_login_rate(ip: str) -> bool:
    now = _login_time.time()
    timestamps = _LOGIN_FAILURES.get(ip, [])
    _LOGIN_FAILURES[ip] = [t for t in timestamps if now - t <= _LOGIN_WINDOW_S]
    return len(_LOGIN_FAILURES[ip]) < _MAX_LOGIN_ATTEMPTS


def _record_login_failure(ip: str) -> None:
    now = _login_time.time()
    if ip not in _LOGIN_FAILURES:
        _LOGIN_FAILURES[ip] = []
    _LOGIN_FAILURES[ip].append(now)


router = APIRouter()


_QR_COOKIE = "blc_qr_browser"
_QR_SECRET = secrets.token_bytes(32)
_QR_COOKIE_SECONDS = 1800


def _qr_browser(request: Request, *, create: bool = False) -> str:
    """只接受本进程签发的短时浏览器标识，不用用户名代替浏览器会话。"""
    token = request.cookies.get(_QR_COOKIE, "")
    if len(token) <= 200:
        payload, separator, signature = token.rpartition(".")
        nonce, _, expires = payload.partition(".")
        expected = hmac.digest(_QR_SECRET, payload.encode(), "sha256").hex()
        if (
            separator
            and len(nonce) == 32
            and all(char in "0123456789abcdef" for char in nonce)
            and expires.isascii()
            and expires.isdecimal()
            and _login_time.time() < int(expires) <= _login_time.time() + _QR_COOKIE_SECONDS
            and signature.isascii()
            and secrets.compare_digest(signature, expected)
        ):
            # 续期不改变浏览器 nonce；已开始的任务仍属于同一浏览器。
            # 签名通过后从数值重建规范文本，不把请求中的原始片段写入响应 Cookie。
            nonce_number = int(nonce, 16)
            expires_at = int(_login_time.time()) + _QR_COOKIE_SECONDS if create else int(expires)
            payload = f"{nonce_number:032x}.{expires_at}"
            return payload + "." + hmac.digest(_QR_SECRET, payload.encode(), "sha256").hex()
    if not create:
        raise HTTPException(status_code=404, detail="登录任务不存在。")
    payload = f"{secrets.token_hex(16)}.{int(_login_time.time()) + _QR_COOKIE_SECONDS}"
    return payload + "." + hmac.digest(_QR_SECRET, payload.encode(), "sha256").hex()


def _qr_owner(request: Request, browser: str | None = None) -> str:
    """任务同时绑定认证身份和短时浏览器标识，切换会话不能访问旧任务。"""
    browser = browser or _qr_browser(request)
    identity = getattr(request.state, "auth_session", "") or request.headers.get("authorization", "")
    nonce = browser.partition(".")[0]
    return sha256(f"{request.state.auth_user}\0{identity}\0{nonce}".encode()).hexdigest()


def _qr_response(token: str, task: LoginTask) -> JSONResponse:
    """统一返回可恢复状态与剩余期限，不包含任何账号凭据。"""
    return JSONResponse(
        {
            "task_id": token,
            "status": task.status,
            "qrcode": task.image,
            "error": task.error,
            "expires_in": qr_logins.remaining(task),
        },
        headers={"Cache-Control": "no-store"},
    )


def _qr_set_browser(response: JSONResponse, browser: str, request: Request) -> None:
    response.set_cookie(
        _QR_COOKIE,
        browser,
        max_age=_QR_COOKIE_SECONDS,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        path="/api/login",
    )


@router.get("/login/qrcode")
async def qr_login_current(request: Request) -> JSONResponse:
    """恢复本浏览器任务；仅签发浏览器标识，不创建或重放登录操作。"""
    browser = _qr_browser(request, create=True)
    current = qr_logins.current(_qr_owner(request, browser))
    response = (
        _qr_response(*current)
        if current
        else JSONResponse({"task_id": None, "status": "idle"}, headers={"Cache-Control": "no-store"})
    )
    _qr_set_browser(response, browser, request)
    return response


@router.post("/login/qrcode")
async def qr_login_start(request: Request) -> JSONResponse:
    """申请网页扫码任务，无需服务器桌面。"""
    from app.web import main

    browser = _qr_browser(request, create=True)
    session = getattr(request.state, "auth_session", "")
    store = main._DOCKER_AUTH
    credential_version = getattr(request.state, "auth_credential_version", "")

    async def authorized() -> bool:
        if store is None:
            return True  # 本机模式继续由路由中间件校验。
        return await asyncio.to_thread(store.admin_authorized, session=session, credential_version=credential_version)

    def persist(cookie: str) -> None:
        from app.web.login_handler import _save_cookie

        if store is None:
            _save_cookie(cookie)
            return
        try:
            store.run_admin_action(lambda: _save_cookie(cookie), session=session, credential_version=credential_version)
        except AuthError as exc:
            raise QrLoginError(str(exc)) from exc

    try:
        token = qr_logins.start(_qr_owner(request, browser), authorize=authorized, persist=persist)
    except QrLoginError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    task = qr_logins.get(token, _qr_owner(request, browser))
    assert task is not None
    response = _qr_response(token, task)
    _qr_set_browser(response, browser, request)
    return response


@router.get("/login/qrcode/{task_id}")
async def qr_login_status(task_id: str, request: Request) -> JSONResponse:
    """返回二维码和四种平台状态，不返回 Cookie 或刷新口令。"""
    task = qr_logins.get(task_id, _qr_owner(request))
    if task is None:
        raise HTTPException(status_code=404, detail="登录任务不存在。")
    return _qr_response(task_id, task)


@router.post("/login/qrcode/{task_id}/cancel")
async def qr_login_cancel(task_id: str, request: Request) -> JSONResponse:
    """停止当前扫码任务，不清除已保存 Cookie。"""
    if not await qr_logins.cancel(task_id, _qr_owner(request)):
        raise HTTPException(status_code=404, detail="登录任务不存在。")
    task = qr_logins.get(task_id, _qr_owner(request))
    if task is None:
        raise HTTPException(status_code=404, detail="登录任务已过期。")
    return _qr_response(task_id, task)


# ----------------------------- 账号登录 / Cookie 管理 ----------------------------- #
@router.get("/cookie-status")
def cookie_status() -> dict[str, Any]:
    """返回当前 Bilibili Cookie 存续状态。"""
    return get_cookie_info()


@router.post("/login")
def login_start(request: Request) -> dict[str, Any]:
    """启动一次浏览器登录流程（Playwright）,返回任务 ID 供前端轮询。"""
    ip = request.client.host if request.client else "unknown"
    if not _check_login_rate(ip):
        raise HTTPException(status_code=429, detail="登录尝试过于频繁,请稍后再试")
    try:
        return start_login()
    except Exception as exc:
        _record_login_failure(ip)
        raise HTTPException(status_code=500, detail=safe_exception_summary(exc)) from exc


@router.get("/login/status")
def login_status(task_id: int) -> dict[str, Any]:
    """查询登录任务当前状态。

    - ``starting``: 正在启动浏览器
    - ``waiting``: 等待用户在浏览器中完成登录
    - ``done``: 登录成功,Cookie 已保存
    - 含 ``error`` 时表示登录失败
    """
    return get_login_status(task_id)


@router.post("/login/clear")
def login_clear() -> dict[str, str]:
    """清除已保存的 Bilibili Cookie。"""
    from app.core import settings_store

    settings_store.set_setting("bilibili_cookie", "")
    return {"status": "cleared"}
