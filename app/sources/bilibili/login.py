"""Bilibili WEB 扫码协议：固定服务地址、严格校验凭据，不依赖服务器桌面。"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import quote, unquote, urlsplit

import httpx

_PASSPORT = "https://passport.bilibili.com/x/passport-login/web/qrcode"
_NAV = "https://api.bilibili.com/x/web-interface/nav"
_HEADERS = {
    "Referer": "https://www.bilibili.com",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
    ),
}
_MAX_RESPONSE = 65536


class QrState(StrEnum):
    """WEB 渠道的扫码、确认、凭据验证、过期和完成事件。"""

    SCAN = "scan"
    CONF = "confirm"
    VERIFY = "validating"
    TIMEOUT = "timeout"
    DONE = "done"


class QrLoginError(RuntimeError):
    """不包含二维码密钥或账号凭据的协议错误。"""


class QrLoginUnavailable(QrLoginError):
    """平台短暂网络故障；仅允许有限重试固定地址的只读请求。"""


def _passport_url(value: object, *, qrcode: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        raise QrLoginError("Bilibili 返回了无效的登录地址。")
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname
            in (
                {"passport.bilibili.com", "account.bilibili.com"}
                if qrcode
                else {"passport.bilibili.com", "passport.biligame.com"}
            )
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
        )
    except ValueError as exc:
        raise QrLoginError("Bilibili 返回了无效的登录地址。") from exc
    if not valid:
        raise QrLoginError("Bilibili 返回了无效的登录地址。")
    return value


def _cookie_value(value: str) -> str:
    # 保留合法的原始百分号编码，同时拒绝编码后的分隔符和控制字符。
    if not value or len(value) > 4096 or re.search(r"%(?![0-9a-fA-F]{2})", value):
        raise QrLoginError("Bilibili 未返回完整的登录凭据。")
    decoded = unquote(value, errors="strict")
    if any(ord(c) < 33 or ord(c) > 126 or c in ';\\"' for c in decoded):
        raise QrLoginError("Bilibili 返回了无效的登录凭据。")
    return quote(value, safe="%")


@dataclass(repr=False)
class WebQrLogin:
    """通过注入的 HTTP 客户端获取二维码、轮询并验证账号，不启动浏览器。"""

    client: httpx.AsyncClient
    url: str = field(default="", repr=False)
    _key: str = field(default="", repr=False)
    _candidate: dict[str, str] | None = field(default=None, repr=False)
    _credential: dict[str, str] | None = field(default=None, repr=False)

    async def _request(self, url: str, *, params: dict[str, str] | None = None, cookie: str = "") -> dict[str, object]:
        for attempt in range(3):
            try:
                return await self._request_once(url, params=params, cookie=cookie)
            except QrLoginUnavailable:
                if attempt == 2:
                    raise
                await asyncio.sleep(0.5 * 2**attempt)
        raise AssertionError("unreachable request retry state")

    async def _request_once(
        self, url: str, *, params: dict[str, str] | None = None, cookie: str = ""
    ) -> dict[str, object]:
        try:
            headers = {**_HEADERS, **({"Cookie": cookie} if cookie else {})}
            async with self.client.stream(
                "GET", url, params=params, headers=headers, follow_redirects=False
            ) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > _MAX_RESPONSE:
                        raise QrLoginError("Bilibili 登录接口响应过大。")
                payload = json.loads(body)
        except httpx.TransportError as exc:
            # HTTP 异常通常带请求 URL，其中可能包含 qrcode_key；不向界面暴露异常链。
            raise QrLoginUnavailable("Bilibili 暂时无法连接，请检查网络后重试。") from exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429 or exc.response.status_code >= 500:
                raise QrLoginUnavailable("Bilibili 暂时不可用，请稍后重试。") from exc
            raise QrLoginError("Bilibili 登录接口拒绝请求，请重新申请二维码。") from exc
        except (httpx.DecodingError, ValueError) as exc:
            raise QrLoginError("Bilibili 登录接口返回了无效数据。") from exc
        if not isinstance(payload, dict) or type(payload.get("code")) is not int or payload["code"] != 0:
            raise QrLoginError("Bilibili 登录接口返回错误，未修改已保存的 Cookie。")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise QrLoginError("Bilibili 登录接口返回的数据不完整。")
        return data

    async def generate_qrcode(self) -> None:
        """申请 WEB 二维码；地址必须属于固定 HTTPS 登录域。"""
        self._candidate = self._credential = None
        self._key = self.url = ""
        data = await self._request(f"{_PASSPORT}/generate", params={"source": "main-fe-header"})
        key = data.get("qrcode_key")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", key):
            raise QrLoginError("Bilibili 未返回有效的二维码。")
        self.url, self._key = _passport_url(data.get("url"), qrcode=True), key

    def get_credential(self) -> dict[str, str]:
        """仅在扫码确认并成功验证账号后返回凭据副本。"""
        if self._credential is None:
            raise QrLoginError("尚未完成扫码确认和账号验证。")
        return dict(self._credential)

    async def check_state(self) -> QrState:
        """严格识别成功码；未知状态和不完整凭据绝不表示登录完成。"""
        if not self._key:
            raise QrLoginError("请先申请二维码。")
        data = await self._request(f"{_PASSPORT}/poll", params={"qrcode_key": self._key})
        code = data.get("code")
        if type(code) is not int:
            raise QrLoginError("Bilibili 返回了无效的扫码状态。")
        state = {86101: QrState.SCAN, 86090: QrState.CONF, 86038: QrState.TIMEOUT}.get(code)
        if state is not None:
            return state
        if code != 0:
            raise QrLoginError("Bilibili 未确认登录成功，请重新申请二维码。")
        callback = _passport_url(data.get("url"))
        fields: dict[str, str] = {}
        # 保留 query 值的原始百分号编码，拒绝重复或大小写冲突的凭据字段。
        for item in urlsplit(callback).query.split("&"):
            name, separator, value = item.partition("=")
            name = name.lower()
            if separator and name in {"sessdata", "bili_jct", "dedeuserid"}:
                if name in fields:
                    raise QrLoginError("Bilibili 返回了歧义登录凭据。")
                fields[name] = value
        # 成功响应也可通过 Set-Cookie 下发凭据；回调地址不一定含账号字段。
        # 仅补齐缺失字段，已出现的无效 query 值仍由下面的校验拒绝。
        cookie_fields: dict[str, str] = {}
        for item in self.client.cookies.jar:
            domain = item.domain.lower().lstrip(".")
            name = item.name.lower()
            if name not in {"sessdata", "bili_jct", "dedeuserid"} or not (
                domain == "bilibili.com" or domain.endswith(".bilibili.com")
            ):
                continue
            if name in cookie_fields and cookie_fields[name] != item.value:
                raise QrLoginError("Bilibili 返回了歧义登录凭据。")
            cookie_fields[name] = item.value
        for name, value in cookie_fields.items():
            fields.setdefault(name, value)
        uid = fields.get("dedeuserid", "")
        csrf = fields.get("bili_jct", "")
        if not re.fullmatch(r"[1-9][0-9]{0,19}", uid) or not re.fullmatch(r"[0-9a-fA-F]{32}", csrf):
            raise QrLoginError("Bilibili 未返回完整的登录凭据。")
        try:
            credential = {
                "SESSDATA": _cookie_value(fields.get("sessdata", "")),
                "bili_jct": csrf,
                "DedeUserID": uid,
            }
            refresh = data.get("refresh_token")
            if refresh is not None and not isinstance(refresh, str):
                raise QrLoginError("Bilibili 返回了无效的刷新凭据。")
            if refresh:
                credential["ac_time_value"] = _cookie_value(refresh)
        except UnicodeError as exc:
            raise QrLoginError("Bilibili 返回了无效的登录凭据编码。") from exc
        self._candidate = credential
        return QrState.VERIFY

    async def validate_credential(self) -> None:
        """仅向固定账号接口提交候选 Cookie，并核对实际登录 UID。"""
        candidate = self._candidate
        if candidate is None:
            raise QrLoginError("尚未完成扫码确认。")
        try:
            cookie = "; ".join(f"{key}={value}" for key, value in candidate.items())
            data = await self._request(_NAV, cookie=cookie)
            uid = data.get("mid")
            if data.get("isLogin") is not True or type(uid) is not int or str(uid) != candidate["DedeUserID"]:
                raise QrLoginError("账号验证失败，未修改已保存的 Cookie。")
            self._credential = dict(candidate)
        finally:
            self._candidate = None
