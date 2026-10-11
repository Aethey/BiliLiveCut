"""系统 MIME 映射污染时的真实静态资源 HTTP 回归。"""

from __future__ import annotations

import mimetypes
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.web.staticfiles import WebStaticFiles


@pytest.mark.parametrize(
    ("suffix", "expected"), [(".js", "text/javascript"), (".mjs", "text/javascript"), (".css", "text/css")]
)
def test_static_files_override_polluted_mime_and_keep_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str, expected: str
) -> None:
    mimetypes.init()
    monkeypatch.setitem(mimetypes.types_map, suffix, "text/plain")
    source = b"/* asset */"
    (tmp_path / f"asset{suffix}").write_bytes(source)
    app = FastAPI()
    app.mount("/static", WebStaticFiles(directory=tmp_path))
    with TestClient(app) as client:
        response = client.get(f"/static/asset{suffix}")
        assert response.status_code == 200
        assert response.content == source
        assert response.headers["content-type"] == f"{expected}; charset=utf-8"
        assert response.headers["x-content-type-options"] == "nosniff"
        head = client.head(f"/static/asset{suffix}")
        assert head.status_code == 200 and head.content == b""
        assert head.headers["content-type"] == response.headers["content-type"]
        part = client.get(f"/static/asset{suffix}", headers={"Range": "bytes=0-3"})
        assert part.status_code == 206 and part.content == source[:4]
        assert part.headers["content-type"] == response.headers["content-type"]
        cached = client.get(f"/static/asset{suffix}", headers={"If-None-Match": response.headers["etag"]})
        assert cached.status_code == 304
        assert cached.content == b""
        missing = client.get(f"/static/missing{suffix}")
        assert missing.status_code == 404
        assert missing.headers["content-type"] == "application/json"
        assert client.get("/static/%2e%2e/outside.js").status_code == 404
        (tmp_path / "image.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        assert client.get("/static/image.svg").headers["content-type"] == "image/svg+xml"


def test_production_mount_serves_all_modules_with_javascript_mime(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.web.main import app

    mimetypes.init()
    monkeypatch.setitem(mimetypes.types_map, ".js", "text/plain")
    root = Path(__file__).resolve().parents[2] / "app/web/static"
    # 不启动应用 lifespan，避免访问运行中的录制、模型或数据库。
    client = TestClient(app)
    for source in sorted(root.rglob("*.js")):
        response = client.get(f"/static/{source.relative_to(root).as_posix()}")
        assert response.status_code == 200, source
        assert response.headers["content-type"] == "text/javascript; charset=utf-8", source
        assert response.content == source.read_bytes()
