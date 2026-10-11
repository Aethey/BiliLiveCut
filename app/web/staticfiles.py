"""为 Web UI 的模块和样式提供不受系统 MIME 注册表影响的静态响应。"""

from __future__ import annotations

import os
from pathlib import Path

from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope


class WebStaticFiles(StaticFiles):
    """保留静态文件路径和缓存校验，只固定浏览器必须识别的资源类型。"""

    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        """在真实文件响应上指定模块及样式 MIME，不将错误页面伪装成脚本。"""
        response = super().file_response(full_path, stat_result, scope, status_code)
        media_type = {".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css"}.get(
            Path(full_path).suffix.lower()
        )
        if media_type and 200 <= response.status_code < 300:
            response.headers["content-type"] = f"{media_type}; charset=utf-8"
        response.headers["x-content-type-options"] = "nosniff"
        return response
