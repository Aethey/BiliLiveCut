"""CLI 子命令 — 系统诊断。"""

from __future__ import annotations

import subprocess as _subprocess

import typer
from rich.console import Console

from app.core.sanitize import safe_exception_summary

console = Console()


def cmd_doctor(
    yes: bool = typer.Option(False, "--yes", help="跳过交互确认"),
) -> None:
    """自检命令 — 检查系统环境与依赖是否满足运行要求。

    检查项:
    - Python 版本、FFmpeg/FFprobe
    - 数据库状态 (Schema version/fingerprint/integrity)
    - 磁盘空间、数据目录权限
    - 配置安全性 (ADMIN_PASSWORD)
    - Bilibili API 连通性
    - CPU/GPU 资源

    输出等级: PASS / WARN / FAIL。
    存在 FAIL 时退出码非 0。

    :param yes: 跳过交互确认。
    """
    import os as _os
    import shutil
    import sys as _sys
    import tempfile
    from pathlib import Path as _Path

    from sqlalchemy.engine import make_url

    from app.core.config import settings as _cfg

    results: list[dict] = []

    def report(level: str, item: str, detail: str = "") -> None:
        color = {"PASS": "green", "WARN": "yellow", "FAIL": "red"}.get(level, "white")
        symbol = {"PASS": "✓", "WARN": "⚠", "FAIL": "✗"}.get(level, "?")
        console.print(f"[{color}]  {symbol}[/{color}] {item}" + (f"  — {detail}" if detail else ""))
        results.append({"level": level, "item": item, "detail": detail})

    console.print("[bold]BiliLiveCut Doctor[/bold]\n")

    # ── Python 版本
    ver = _sys.version_info
    if (ver.major, ver.minor) >= (3, 11):
        report("PASS", "Python 版本", f"{ver.major}.{ver.minor}.{ver.micro}")
    else:
        report("FAIL", "Python 版本", f"{ver.major}.{ver.minor}.{ver.micro} (需 ≥ 3.11)")

    # ── FFmpeg / FFprobe
    ffmpeg = shutil.which(_cfg.ffmpeg_path)
    ffprobe = shutil.which(_cfg.ffprobe_path)
    if ffmpeg:
        report("PASS", "FFmpeg", ffmpeg)
    else:
        report("FAIL", "FFmpeg", "未找到, 录制/渲染不可用")
    if ffprobe:
        report("PASS", "FFprobe", ffprobe)
    else:
        report("WARN", "FFprobe", "未找到, 部分媒体分析功能不可用")

    # ── 数据库
    database = make_url(_cfg.database_url).database
    db_path = _Path(database) if database and database != ":memory:" else None
    if db_path is not None and db_path.exists():
        try:
            from app.db.schema import (
                CURRENT_SCHEMA_VERSION,
                validate_schema,
            )

            ok = validate_schema()
            if ok:
                report("PASS", "数据库 Schema", f"v{CURRENT_SCHEMA_VERSION}")
            else:
                report("FAIL", "数据库 Schema", "不兼容, 需重建")
            # Integrity
            from app.db.session import engine

            with engine.connect() as conn:
                r = conn.exec_driver_sql("PRAGMA integrity_check").fetchone()
                if r and r[0] == "ok":
                    report("PASS", "数据库完整性")
                else:
                    report("FAIL", "数据库完整性", str(r[0]) if r else "unknown")
        except Exception as exc:
            report("FAIL", "数据库检查", safe_exception_summary(exc, limit=300))
    else:
        report("WARN", "数据库", "尚未创建 (首次启动时自动创建)")

    # ── 磁盘空间
    data_dir = _Path(_cfg.storage_root)
    if data_dir.exists():
        try:
            if hasattr(shutil, "disk_usage"):
                usage = shutil.disk_usage(data_dir)
                free_gb = usage.free / (1024**3)
                if free_gb >= 5:
                    report("PASS", "磁盘空间", f"{free_gb:.1f} GB 可用")
                elif free_gb >= 2:
                    report("WARN", "磁盘空间", f"仅 {free_gb:.1f} GB 可用")
                else:
                    report("FAIL", "磁盘空间", f"严重不足: {free_gb:.1f} GB")
        except OSError as exc:
            report("WARN", "磁盘空间", safe_exception_summary(exc))

    # ── 数据目录权限
    try:
        with tempfile.TemporaryFile(dir=data_dir, prefix=".doctor-", mode="w") as test_file:
            test_file.write("test")
        report("PASS", "数据目录权限", str(data_dir))
    except OSError as exc:
        report("FAIL", "数据目录权限", f"无法写入 {data_dir}: {safe_exception_summary(exc)}")

    # ── ADMIN_PASSWORD 安全
    if not _cfg.admin_password:
        import socket

        hostname = socket.gethostname()
        has_nonloop = False
        try:
            for info in socket.getaddrinfo(hostname, None):
                addr = info[4][0]
                if not addr.startswith("127.") and addr != "::1":
                    has_nonloop = True
                    break
        except Exception:
            has_nonloop = True
        if has_nonloop:
            report("WARN", "Web 安全", "ADMIN_PASSWORD 为空, 非本机访问将被拒绝")
        else:
            report("WARN", "Web 安全", "ADMIN_PASSWORD 为空 (仅本机可访问)")

    # ── Bilibili API 连通性 (可选)
    try:
        import httpx

        resp = httpx.get("https://api.live.bilibili.com/", timeout=5)
        if resp.status_code < 500:
            report("PASS", "Bilibili API", "连通正常")
        else:
            report("WARN", "Bilibili API", f"HTTP {resp.status_code}")
    except Exception as exc:
        report("WARN", "Bilibili API", f"无法连通: {safe_exception_summary(exc)}")

    # ── CPU/Memory
    try:
        cpu_count = _os.cpu_count() or 0
        report("PASS", "CPU", f"{cpu_count} 核")
    except Exception:
        report("WARN", "CPU", "无法检测")

    # ── GPU
    try:
        from app.core.asr_detection import detect_resources

        res = detect_resources()
        if res.gpu_available:
            vram = res.vram_total_mb / 1024
            report("PASS", "GPU 硬件", f"NVIDIA ({vram:.1f} GB VRAM)；ASR 后端可用性另行检查")
        else:
            report("WARN", "GPU 硬件", "nvidia-smi 未检测到 NVIDIA GPU；不会因此改写 ASR 设备配置")
    except Exception as exc:
        report("WARN", "GPU", safe_exception_summary(exc))

    # ── ASR 配置
    asr_backend = _cfg.asr_primary
    report("PASS" if asr_backend != "none" else "WARN", "ASR 后端", asr_backend or "未配置")

    # CUDA 配置、NVIDIA 硬件与推理后端的实际可用性是三个不同事实。
    from app.core.asr_devices import probe_device

    roles: list[tuple[str, str, str]] = []
    if asr_backend in {"funasr_nano", "paraformer"}:
        roles.append(("primary", "torch", _cfg.asr_primary_device))
        if _cfg.asr_sensevoice and _cfg.asr_sensevoice_enabled:
            roles.append(("auxiliary", "torch", _cfg.asr_auxiliary_device))
        if asr_backend == "paraformer" and _cfg.asr_funasr_review:
            roles.append(("review", "torch", _cfg.asr_review_device))
    if asr_backend == "whisper":
        roles.append(("fallback (Whisper 主引擎)", "ctranslate2", _cfg.asr_fallback_device))
    elif _cfg.asr_fallback_whisper:
        roles.append(("fallback", "ctranslate2", _cfg.asr_fallback_device))
    cache: dict[tuple[str, str], dict[str, str | bool | None]] = {}
    for role, backend, device in roles:
        device = device or _cfg.whisper_device
        try:
            key = (backend, device)
            if key not in cache:
                cache[key] = probe_device(backend, device)
            actual = cache[key]
            detail = ", ".join(f"{key}={value}" for key, value in actual.items())
            report(
                "PASS" if actual["ok"] else ("FAIL" if device.startswith("cuda") else "WARN"),
                f"ASR 设备 {role}",
                detail,
            )
        except (OSError, ValueError, _subprocess.SubprocessError) as exc:
            report("FAIL" if device.startswith("cuda") else "WARN", f"ASR 设备 {role}", safe_exception_summary(exc))

    # ── 模型目录
    configured_models = _os.environ.get("BLC_MODELS_DIR", "")
    model_dir = _Path(configured_models) if configured_models else None
    if asr_backend == "openai_compatible" and not _cfg.asr_fallback_whisper:
        report("PASS", "模型目录", "远程转写且未启用本地回退，无需本地转写模型")
    elif model_dir and model_dir.exists():
        report("PASS", "模型目录", str(model_dir))
    elif model_dir:
        report("WARN", "模型目录", "不存在")
    else:
        report("WARN", "模型目录", "未设置 BLC_MODELS_DIR，将使用模型 SDK 缓存")

    # ── Uploader
    if db_path is not None and db_path.exists():
        from app.core import settings_store

        try:
            report("PASS", "上传方式", "biliup" if settings_store.biliup_enabled() else "manual")
        except Exception as exc:  # noqa: BLE001 — 诊断逐项汇总，不因坏库丢失前面的结果。
            report("FAIL", "上传方式", safe_exception_summary(exc))
    else:
        report("WARN", "上传方式", "数据库尚未初始化，运行时开关未知")

    # ── 汇总
    console.print("")
    fail_count = sum(1 for r in results if r["level"] == "FAIL")
    warn_count = sum(1 for r in results if r["level"] == "WARN")
    pass_count = sum(1 for r in results if r["level"] == "PASS")

    if fail_count == 0:
        console.print(f"[green]{pass_count} PASS, {warn_count} WARN[/green] — 环境就绪")
    else:
        console.print(f"[red]{fail_count} FAIL, {warn_count} WARN, {pass_count} PASS[/red] — 存在关键问题需修复")
        raise typer.Exit(code=1)


# 注册列表
DOCTOR_COMMANDS = [
    ("doctor", cmd_doctor, None),
]
