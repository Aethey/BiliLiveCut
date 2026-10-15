"""Docker 浏览器认证入口；保留显式 Basic API 客户端和审核员权限边界。"""

from __future__ import annotations

import base64
import json
import secrets
import sqlite3
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

from loguru import logger
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.templating import Jinja2Templates

from app.core.docker_auth import COOKIE_NAME, AuthError, AuthStore

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
_WRITES = {"POST", "PUT", "PATCH", "DELETE"}


def _json(content: dict[str, object], status: int = 200) -> JSONResponse:
    return JSONResponse(content, status_code=status, headers={"Cache-Control": "no-store"})


def _csrf_required() -> Response:
    response = _json({"detail": "登录状态已更新，本次操作未执行，请确认后重试。"}, 403)
    response.headers["X-BLC-CSRF-Required"] = "1"
    return response


def _clear(response: Response) -> Response:
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="strict")
    return response


def _required(request: Request, detail: str = "登录已失效，请重新登录。") -> Response:
    if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
        # 目标仅来自本站路径；前端再校验同源，防止开放重定向。
        target = request.url.path + ("?" + request.url.query if request.url.query else "")
        response = RedirectResponse("/auth/login?next=" + quote(target, safe=""), status_code=303)
    else:
        response = _json({"detail": detail}, 401)
    response.headers["X-BLC-Auth-Required"] = "1"
    response.headers["Cache-Control"] = "no-store"
    # 延迟到达的旧 401 不得删除刚登录的新 Cookie；仅显式退出/改密清理 Cookie。
    return response


async def _body(request: Request) -> dict[str, str | bool]:
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise AuthError("请使用 JSON 提交登录表单。", 415)
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 4096:
            raise AuthError("登录请求过大。", 413)
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise AuthError("登录请求格式无效。") from exc
    if not isinstance(value, dict) or any(not isinstance(v, (str, bool)) for v in value.values()):
        raise AuthError("登录请求字段无效。")
    return value


def _field(body: dict[str, str | bool], key: str, maximum: int = 1024) -> str:
    value = body.get(key, "")
    if not isinstance(value, str) or len(value) > maximum:
        raise AuthError("登录请求字段无效。")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise AuthError("登录请求包含无效字符。") from exc
    return value


async def _endpoint(
    request: Request,
    store: AuthStore,
    reviewers: dict[str, str],
    session: dict[str, str] | None,
    csrf_check: Callable[[Request], bool],
) -> Response:
    path = request.url.path
    token = request.cookies.get(COOKIE_NAME, "")
    if path == "/auth/status" and request.method == "GET":
        state = await run_in_threadpool(store.state)
        return _json({**state, "authenticated": session is not None, **(session or {})})
    if path in {"/auth/login", "/auth/password"} and request.method == "GET":
        if path == "/auth/password" and session is None:
            return _required(request)
        response = _TEMPLATES.TemplateResponse(request, "admin_auth.html", {"password_page": path == "/auth/password"})
        response.headers.update(
            {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY"}
        )
        return response
    if request.method != "POST" or path not in {"/auth/login", "/auth/setup", "/auth/logout", "/auth/password"}:
        return _json({"detail": "认证入口不存在。"}, 404)
    # 登录、初始化也禁止跨站提交，防止登录 CSRF；此 API 不使用跨域凭据。
    if not request.headers.get("origin") or not csrf_check(request):
        return _json({"detail": "登录操作必须来自本站页面。"}, 403)
    body = await _body(request)
    ip = request.client.host if request.client else "unknown"
    if path == "/auth/setup":
        await run_in_threadpool(store.initialize, _field(body, "code", 128), _field(body, "password", 128), ip)
        return _json({"detail": "密码设置成功，请登录。"})
    if path == "/auth/login":
        username = _field(body, "username", 128)
        result = await run_in_threadpool(
            store.authenticate, username, _field(body, "password"), reviewers, ip, create_session=True
        )
        if result is None:
            return _json({"detail": "账号或密码不正确。"}, 401)
        # 避免在登录接口形成会话固定；旧浏览器会话也被撤销。
        await run_in_threadpool(store.logout, token)
        response = _json({"detail": "登录成功。", "role": result["role"], "username": username})
        response.set_cookie(
            COOKIE_NAME,
            result["token"],
            max_age=store.session_seconds,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
            path="/",
        )
        return response
    if session is None:
        return _required(request)
    if not secrets.compare_digest(request.headers.get("x-blc-csrf", "").encode(), session["csrf_token"].encode()):
        return _csrf_required()
    if path == "/auth/logout":
        await run_in_threadpool(store.logout, token, everywhere=body.get("everywhere") is True)
        return _clear(_json({"detail": "已退出登录。"}))
    if session["role"] != "admin":
        return _json({"detail": "只有管理员可以修改后台密码。"}, 403)
    await run_in_threadpool(
        store.change_password, token, _field(body, "current_password"), _field(body, "password", 128), ip
    )
    return _clear(_json({"detail": "密码已修改，所有旧会话已退出，请使用新密码登录。"}))


async def dispatch(
    request: Request,
    call_next: RequestResponseEndpoint,
    store: AuthStore,
    reviewers: dict[str, str],
    csrf_check: Callable[[Request], bool],
    reviewer_allowed: Callable[[str], bool],
) -> Response:
    """将 Docker 的管理页面、媒体与接口统一放在会话或 Basic 认证之后。"""
    path = request.url.path
    if path.startswith("/static/"):
        return await call_next(request)
    try:
        session = await run_in_threadpool(
            store.session, request.cookies.get(COOKIE_NAME, ""), touch=not path.startswith("/auth/")
        )
        if path.startswith("/auth/"):
            return await _endpoint(request, store, reviewers, session, csrf_check)
        # 浏览器可能缓存升级前的 Basic 凭据；浏览器统一使用会话，避免旧凭据绕过退出/超时。
        # Fetch Metadata 由浏览器设置；脚本 API 客户端不带此头时继续使用显式 Basic。
        authorization = "" if request.headers.get("sec-fetch-site") else request.headers.get("authorization", "")
        if authorization:
            try:
                if not authorization.startswith("Basic ") or len(authorization) > 4096:
                    raise ValueError("invalid authorization")
                username, separator, password = (
                    base64.b64decode(authorization[6:], validate=True).decode().partition(":")
                )
                if not separator:
                    raise ValueError("invalid authorization")
            except (ValueError, UnicodeError):
                return _json({"detail": "认证格式错误。"}, 400)
            result = await run_in_threadpool(
                store.authenticate, username, password, reviewers, request.client.host if request.client else "unknown"
            )
            if result is None:
                return _json({"detail": "认证失败。"}, 401)
            role = result["role"]
            request.state.auth_credential_version = result["credential_version"]
        elif session is not None:
            username, role = session["username"], session["role"]
            request.state.auth_session = request.cookies.get(COOKIE_NAME, "")
            if request.method in _WRITES:
                if not secrets.compare_digest(
                    request.headers.get("x-blc-csrf", "").encode(), session["csrf_token"].encode()
                ):
                    return _csrf_required()
        else:
            return _required(request)
        if role == "reviewer" and not reviewer_allowed(path):
            return _json({"detail": "审核员无权访问该管理接口。"}, 403)
        if request.method in _WRITES and not csrf_check(request):
            return _json({"detail": "跨站请求被拒绝。"}, 403)
        request.state.auth_user, request.state.auth_role = username, role
    except AuthError as exc:
        return _json({"detail": str(exc)}, exc.status)
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        # 认证库损坏/丢失必须失败关闭，不能退回无密码模式；不输出凭据和数据库内容。
        logger.error("docker_auth_storage_failed: {} path={}", type(exc).__name__, store.path)
        return _json({"detail": "认证存储不可用，请检查持久化目录、权限和容器启动日志。"}, 503)

    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    return response
