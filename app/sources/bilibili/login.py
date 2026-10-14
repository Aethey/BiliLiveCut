"""Bilibili WEB 扫码登录协议，不依赖服务器桌面。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import quote, urlsplit

import httpx

_PASSPORT = "https://passport.bilibili.com/x/passport-login/web/qrcode"
_HEADERS = {
    "Referer": "https://www.bilibili.com",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
    ),
}


class QrState(StrEnum):
    """WEB 渠道的扫码、确认、过期和完成事件。"""

    SCAN = "scan"
    CONF = "confirm"
    TIMEOUT = "timeout"
    DONE = "done"


class QrLoginError(RuntimeError):
    """不包含二维码密钥或账号凭据的协议错误。"""


@dataclass(repr=False)
class WebQrLogin:
    """通过注入的 HTTP 客户端获取二维码并轮询，不启动浏览器。"""

    client: httpx.AsyncClient
    url: str = field(default="", repr=False)
    _key: str = field(default="", repr=False)
    _credential: dict[str, str] | None = field(default=None, repr=False)

    async def _request(self, action: str, **params: str) -> dict:
        try:
            response = await self.client.get(f"{_PASSPORT}/{action}", params=params, headers=_HEADERS)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # HTTP 异常通常带请求 URL，其中可能包含 qrcode_key。
            raise QrLoginError("Bilibili 登录接口请求失败，请检查网络后重试。") from exc
        if not isinstance(payload, dict) or payload.get("code") != 0:
            raise QrLoginError("Bilibili 登录接口返回错误，未修改已保存的 Cookie。")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise QrLoginError("Bilibili 登录接口返回的数据不完整。")
        return data

    async def generate_qrcode(self) -> None:
        """申请 WEB 渠道的二维码和轮询密钥。"""
        data = await self._request("generate", source="main-fe-header")
        if not isinstance(data.get("url"), str) or not isinstance(data.get("qrcode_key"), str):
            raise QrLoginError("Bilibili 未返回有效的二维码。")
        if not data["url"] or not data["qrcode_key"]:
            raise QrLoginError("Bilibili 未返回有效的二维码。")
        self.url, self._key = data["url"], data["qrcode_key"]

    def get_credential(self) -> dict[str, str]:
        """仅在完成扫码确认后返回账号 Cookie。"""
        if self._credential is None:
            raise QrLoginError("尚未完成扫码确认。")
        return dict(self._credential)

    async def check_state(self) -> QrState:
        """返回平台事件；扫码未确认不保存 Cookie，过期不自动申请新码。"""
        if not self._key:
            raise QrLoginError("请先申请二维码。")
        data = await self._request("poll", qrcode_key=self._key)
        state = {86101: QrState.SCAN, 86090: QrState.CONF, 86038: QrState.TIMEOUT}.get(data.get("code"))
        if state is not None:
            return state
        # 其余事件从平台成功回调提取凭据，HTTP 状态本身不表示登录完成。
        callback, refresh = data.get("url"), data.get("refresh_token")
        if not isinstance(callback, str) or "?" not in callback or not isinstance(refresh, str):
            raise QrLoginError("Bilibili 未返回完整的登录凭据。")
        fields = {}
        # 保留 query 值的原始百分号编码，避免 SESSDATA 被重复编码。
        for item in urlsplit(callback).query.split("&"):
            name, separator, value = item.partition("=")
            if separator:
                fields[name] = value
        session = fields.get("SESSDATA", "")
        uid = ""
        for name, value in fields.items():
            if name.lower() == "dedeuserid":
                uid = value
        self._credential = {
            "SESSDATA": session if "%" in session else quote(session),
            "buvid3": "",
            "buvid4": "",
            "bili_jct": fields.get("bili_jct", ""),
            "ac_time_value": refresh,
        }
        if uid:
            self._credential["DedeUserID"] = uid
        self._credential["sessdata"] = self._credential["SESSDATA"]
        self._credential["dedeuserid"] = uid
        return QrState.DONE
