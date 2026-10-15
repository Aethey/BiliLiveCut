"""仅供隔离浏览器/容器验收的 Bilibili HTTP 边界夹具，不进入应用包。"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from typing import Literal
from unittest.mock import patch

import httpx
import uvicorn
from fastapi import Request
from pydantic import BaseModel
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response


class Scenario(BaseModel):
    """隔离测试支持的固定平台响应，不接受任意远端地址。"""

    state: Literal["scan", "confirm", "done", "invalid", "timeout"]


def serve() -> None:
    """显式测试开关下启动真实应用，只有外部 Bilibili HTTP 响应由夹具提供。"""
    if os.environ.get("BLC_QR_BROWSER_FIXTURE") != "1":
        raise RuntimeError("This isolated fixture requires BLC_QR_BROWSER_FIXTURE=1")
    from app.core.docker_auth import COOKIE_NAME
    from app.web import main, qr_login

    assert main._DOCKER_AUTH is not None
    main._DOCKER_AUTH.boot(os.environ["ADMIN_PASSWORD"])
    scenario: dict[str, str | int] = {"state": "scan", "generates": 0, "cancel_posts": 0}

    @main.app.post("/api/qr-fixture")
    async def change(body: Scenario) -> dict[str, str | int]:
        scenario["state"] = body.state
        return dict(scenario)

    @main.app.get("/api/qr-fixture")
    async def current() -> dict[str, str | int]:
        return dict(scenario)

    @main.app.post("/api/qr-fixture/expire")
    async def expire(request: Request) -> dict[str, bool]:
        assert main._DOCKER_AUTH is not None
        await asyncio.to_thread(main._DOCKER_AUTH.logout, request.cookies.get(COOKIE_NAME, ""))
        return {"expired": True}

    @main.app.middleware("http")
    async def count(request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method == "POST" and request.url.path.endswith("/cancel"):
            scenario["cancel_posts"] = int(scenario["cancel_posts"]) + 1
        return await call_next(request)

    def reply(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("generate"):
            scenario["generates"] = int(scenario["generates"]) + 1
            data = {
                "url": "https://account.bilibili.com/h5/account-h5/auth/scan-web?k=fixture",
                "qrcode_key": "fixture",
            }
        elif request.url.path.endswith("/nav"):
            data = {"isLogin": True, "mid": 123}
        elif scenario["state"] == "done":
            data = {
                "code": 0,
                "url": "https://passport.bilibili.com/callback?SESSDATA=browser-fixture-only&bili_jct="
                + "a" * 32
                + "&DedeUserID=123",
            }
        else:
            data = {"code": {"scan": 86101, "confirm": 86090, "invalid": 999, "timeout": 86038}[str(scenario["state"])]}
        return httpx.Response(200, json={"code": 0, "data": data})

    def client(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(reply), **kwargs)

    with patch.object(qr_login, "httpx", SimpleNamespace(AsyncClient=client)):
        uvicorn.run(
            main.app,
            host=os.environ.get("BLC_QR_FIXTURE_HOST", "127.0.0.1"),
            port=int(os.environ.get("BLC_QR_FIXTURE_PORT", "18780")),
            access_log=False,
        )


if __name__ == "__main__":
    serve()
