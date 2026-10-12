"""命令行入口 — 调度器。

所有命令实现已迁移到 app/commands/ 子模块, 本文件仅负责:
- 创建 Typer 主应用
- 从子模块导入并注册命令
- 全局回调 (日志初始化)
- 顶层异常处理
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from functools import wraps
from typing import ParamSpec, TypeVar

import click
import typer
from loguru import logger

from app import __version__
from app.core.logging import setup_logging
from app.core.sanitize import safe_exception_summary

P = ParamSpec("P")
R = TypeVar("R")


def _diagnostic_command(command: Callable[P, R]) -> Callable[P, R]:
    """统一命令异常出口，保留安全日志并避免 Typer 输出凭据与局部变量。"""

    @wraps(command)
    def invoke(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return command(*args, **kwargs)
        except (click.ClickException, click.exceptions.Exit, click.Abort):
            raise
        except Exception as exc:  # noqa: BLE001 — CLI 最外层错误边界
            logger.opt(exception=exc).error("命令失败 command={}", command.__name__)
            typer.echo(safe_exception_summary(exc), err=True)
            raise typer.Exit(code=1) from None

    return invoke


app = typer.Typer(help="BiliLiveCut —— AI 直播实时切片系统 CLI", pretty_exceptions_show_locals=False)


@app.callback()
@_diagnostic_command
def _bootstrap(ctx: typer.Context) -> None:
    """所有命令执行前的初始化: 配置日志。"""
    setup_logging()
    if ctx.invoked_subcommand not in {None, "version", "doctor", "serve", "init"}:
        from app.db.session import init_db

        init_db()


# ── 从子模块注册所有命令 ──────────────────────────────────
from app.commands import ALL_COMMANDS  # noqa: E402

for cmd_name, cmd_func, _help in ALL_COMMANDS:
    # Register each command on the main app
    help_text = _help if _help else cmd_func.__doc__
    app.command(name=cmd_name, help=help_text)(_diagnostic_command(cmd_func))


# ── 版本命令 ─────────────────────────────────────────────
@app.command()
def version() -> None:
    """显示当前版本号。"""
    print(f"BiliLiveCut {__version__}")


def main() -> None:
    """初始化 UTF-8 输出后运行 CLI，覆盖帮助和参数错误等回调前输出。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (OSError, TypeError, ValueError):
            # 测试捕获器或嵌入式宿主可能禁止修改流配置；保留宿主设置。
            continue
    app()


if __name__ == "__main__":
    main()
