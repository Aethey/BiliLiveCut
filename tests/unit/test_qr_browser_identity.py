"""浏览器标识严格解析、签名校验和规范化输出。"""

from __future__ import annotations

import hmac
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request

from app.web.routers import auth


def request_for(token: str) -> Request:
    """构造经过 HTTP 边界的浏览器 Cookie 请求。"""
    return Request({"type": "http", "headers": [(b"cookie", f"blc_qr_browser={token}".encode("ascii"))]})


def signed_token(payload: str) -> str:
    """为格式边界测试构造合法签名，独立验证格式门禁。"""
    return payload + "." + hmac.digest(auth._QR_SECRET, payload.encode(), "sha256").hex()


@pytest.mark.parametrize("nonce", ["0" * 32, "0" * 29 + "a7f", "f" * 32])
@pytest.mark.parametrize("renew", [False, True])
def test_browser_cookie_is_canonical_and_preserves_nonce(
    nonce: str, renew: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auth, "_login_time", SimpleNamespace(time=lambda: 1000.0))
    token = signed_token(f"{nonce}.00002000")
    result = auth._qr_browser(request_for(token), create=renew)
    assert result == signed_token(f"{nonce}.{2800 if renew else 2000}")
    assert "00002000" not in result


@pytest.mark.parametrize("nonce", ["g" * 32, "A" * 32, "-" + "0" * 31, "0" * 31, "0" * 33])
def test_browser_rejects_invalid_nonce_even_with_valid_signature(nonce: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "_login_time", SimpleNamespace(time=lambda: 1000.0))
    with pytest.raises(HTTPException) as caught:
        auth._qr_browser(request_for(signed_token(f"{nonce}.2000")))
    assert caught.value.status_code == 404


def test_browser_rejects_modified_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "_login_time", SimpleNamespace(time=lambda: 1000.0))
    original = signed_token(f"{'1' * 32}.2000")
    forged = "2" + original[1:]
    with pytest.raises(HTTPException) as caught:
        auth._qr_browser(request_for(forged))
    assert caught.value.status_code == 404
