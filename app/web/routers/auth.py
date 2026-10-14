"""登录管理."""

from __future__ import annotations

import time as _login_time
from hashlib import sha256
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.core.sanitize import safe_exception_summary
from app.sources.bilibili.login import QrLoginError
from app.web.login_handler import get_cookie_info, get_login_status, start_login
from app.web.qr_login import qr_logins

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


def _qr_owner(request: Request) -> str:
    """网页登录限定到会话，Basic/本机模式限定到已认证操作者。"""
    identity = getattr(request.state, "auth_session", "") or request.state.auth_user
    return sha256(identity.encode()).hexdigest()


@router.post("/login/qrcode")
async def qr_login_start(request: Request) -> JSONResponse:
    """申请网页扫码任务，无需服务器桌面。"""
    try:
        token = qr_logins.start(_qr_owner(request))
    except QrLoginError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse({"task_id": token, "status": "starting"}, headers={"Cache-Control": "no-store"})


@router.get("/login/qrcode/{task_id}")
async def qr_login_status(task_id: str, request: Request) -> JSONResponse:
    """返回二维码和四种平台状态，不返回 Cookie 或刷新口令。"""
    task = qr_logins.get(task_id, _qr_owner(request))
    if task is None:
        raise HTTPException(status_code=404, detail="登录任务不存在。")
    return JSONResponse(
        {"status": task.status, "qrcode": task.image, "error": task.error},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/login/qrcode/{task_id}/cancel")
async def qr_login_cancel(task_id: str, request: Request) -> JSONResponse:
    """停止当前扫码任务，不清除已保存 Cookie。"""
    if not await qr_logins.cancel(task_id, _qr_owner(request)):
        raise HTTPException(status_code=404, detail="登录任务不存在。")
    task = qr_logins.get(task_id, _qr_owner(request))
    return JSONResponse({"status": task.status}, headers={"Cache-Control": "no-store"})


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
