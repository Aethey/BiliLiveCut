"""供设置中心查看并打开应用管理的本地文件目录。

目录标识采用固定白名单，前端不能传入任意路径。
打开的是运行 Web 服务的计算机上的文件管理器，而非浏览器所在设备。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.osutil import open_path

_FOLDER_INFO = (
    ("storage", "存储根目录", "数据库及全部运行文件"),
    ("raw", "原始录像", "直播录制的原始片段"),
    ("clips", "成品切片", "生成的视频切片"),
    ("ready_to_upload", "待上传文件", "准备上传的成片和素材"),
    ("logs", "日志文件", "运行日志及诊断文件"),
    ("plugins", "插件目录", "本地插件文件"),
)


def _folder_paths() -> dict[str, Path]:
    """根据当前配置计算目录，支持用户修改 STORAGE_ROOT / PLUGIN_DIR。"""
    root = Path(settings.storage_root).expanduser().resolve()
    return {
        "storage": root,
        "raw": root / "raw",
        "clips": root / "clips",
        "ready_to_upload": root / "ready_to_upload",
        "logs": root / "logs",
        "plugins": Path(settings.plugin_dir).expanduser().resolve(),
    }


def list_local_folders() -> list[dict[str, Any]]:
    """列出固定的本地目录，不创建不存在的文件夹。"""
    paths = _folder_paths()
    return [
        {
            "id": key,
            "name": name,
            "description": description,
            "path": str(paths[key]),
            "exists": paths[key].is_dir(),
        }
        for key, name, description in _FOLDER_INFO
    ]


def open_local_folder(folder_id: str) -> dict[str, str]:
    """按白名单打开目录；首次使用时创建目录，失败则明确报错。"""
    path = _folder_paths().get(folder_id)
    if path is None:
        raise ValueError("未知的文件夹类型")
    path.mkdir(parents=True, exist_ok=True)
    if not open_path(path):
        raise OSError("无法打开本机文件管理器；请确认程序运行在有桌面环境的电脑上")
    return {"id": folder_id, "path": str(path)}
