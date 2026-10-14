"""网页扫码任务：短时内存二维码、单账号更新和不可中断的保存边界。"""

from __future__ import annotations

import asyncio
import base64
import io
import secrets
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import httpx
from loguru import logger
from qrcode.exceptions import DataOverflowError
from sqlalchemy.exc import SQLAlchemyError

from app.sources.bilibili.login import QrLoginError, QrState, WebQrLogin


@dataclass
class LoginTask:
    """单次登录的可观察状态和仅服务端持有的异步任务。"""

    owner: str = field(repr=False)
    created: float = field(default_factory=time.monotonic)
    status: str = "starting"
    image: str = field(default="", repr=False)
    error: str = ""
    worker: asyncio.Task[None] | None = field(default=None, repr=False)
    authorize: Callable[[], Awaitable[bool]] | None = field(default=None, repr=False)


class QrLoginManager:
    """账号为全局设置，一次只允许一个更新流程；关闭时等待已开始的持久化。"""

    retention_seconds = 600
    poll_interval = 1.5
    max_tasks = 32

    def __init__(self) -> None:
        self.tasks: dict[str, LoginTask] = {}

    def start(self, owner: str, *, authorize: Callable[[], Awaitable[bool]] | None = None) -> str:
        """创建唯一活动任务，避免不同浏览器轮流覆盖全局账号。"""
        for token, task in list(self.tasks.items()):
            if task.worker is not None and task.worker.done() and self._expired(task):
                del self.tasks[token]
        if any(t.worker is not None and not t.worker.done() for t in self.tasks.values()):
            raise QrLoginError("已有扫码登录正在进行，请在原浏览器完成或取消，或等待二维码过期。")
        if len(self.tasks) >= self.max_tasks:
            raise QrLoginError("登录任务过多，请稍后重试。")
        token = secrets.token_urlsafe(24)
        task = LoginTask(owner, authorize=authorize)
        self.tasks[token] = task
        task.worker = asyncio.create_task(self._run(task))
        return token

    def _expired(self, task: LoginTask) -> bool:
        return time.monotonic() - task.created >= self.retention_seconds

    def get(self, token: str, owner: str) -> LoginTask | None:
        """其他发起者、过期终态和未知 ID 均不能读取任务。"""
        task = self.tasks.get(token)
        if task is None or not secrets.compare_digest(task.owner, owner):
            return None
        if self._expired(task) and task.worker is not None and task.worker.done():
            del self.tasks[token]
            return None
        return task

    @staticmethod
    def _render(url: str) -> str:
        import qrcode
        from qrcode.image.svg import SvgPathImage

        output = io.BytesIO()
        qrcode.make(url, image_factory=SvgPathImage).save(output)
        return "data:image/svg+xml;base64," + base64.b64encode(output.getvalue()).decode("ascii")

    @staticmethod
    async def _authorized(task: LoginTask) -> None:
        if task.authorize is not None and not await task.authorize():
            raise QrLoginError("管理员登录已失效，请重新登录并申请二维码。")

    async def _poll(self, task: LoginTask) -> str | None:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            login = WebQrLogin(client)
            await self._authorized(task)
            await login.generate_qrcode()
            task.image = await asyncio.to_thread(self._render, login.url)
            task.status = QrState.SCAN
            while not self._expired(task):
                await self._authorized(task)
                state = await login.check_state()
                task.status = state
                if state == QrState.VERIFY:
                    task.image = ""
                    await login.validate_credential()
                    await self._authorized(task)
                    return "; ".join(f"{key}={value}" for key, value in login.get_credential().items())
                if state == QrState.TIMEOUT:
                    return None
                await asyncio.sleep(self.poll_interval)
            task.status = QrState.TIMEOUT
            return None

    async def _run(self, task: LoginTask) -> None:
        try:
            async with asyncio.timeout(self.retention_seconds):
                cookie = await self._poll(task)
                await self._authorized(task)
            if cookie is None:
                return
            from app.web.login_handler import _save_cookie

            # 此状态转换至持久化启动之间没有 await。取消/关闭从此只等待结果，
            # 不取消工作线程，不把已经保存的账号报告为 cancelled。
            task.status = "committing"
            await asyncio.to_thread(_save_cookie, cookie)
            task.status = QrState.DONE
        except TimeoutError:
            task.status = QrState.TIMEOUT
        except asyncio.CancelledError:
            task.status = "cancelled"
            raise
        except (
            QrLoginError,
            OSError,
            RuntimeError,
            ValueError,
            sqlite3.Error,
            SQLAlchemyError,
            DataOverflowError,
        ) as exc:
            # 只记录错误类型，不公开异常链、远端 URL、解析结果或数据库参数。
            logger.warning("qr_login_failed: {}", type(exc).__name__)
            task.status = "error"
            task.error = str(exc) if isinstance(exc, QrLoginError) else "扫码登录失败，请检查网络和运行环境后重试。"
        finally:
            task.image = ""
            task.authorize = None

    async def cancel(self, token: str, owner: str) -> bool:
        """保存开始前取消；开始后等待真实保存结果，不清除已保存 Cookie。"""
        task = self.get(token, owner)
        if task is None:
            return False
        if task.worker is not None and not task.worker.done():
            if task.status != "committing":
                task.worker.cancel()
            await asyncio.shield(asyncio.gather(task.worker, return_exceptions=True))
            if task.status not in (QrState.DONE, "error"):
                task.status = "cancelled"
            task.image = ""
        return True

    async def stop(self) -> None:
        """关闭 HTTP 任务并等待已开始的保存；不留后台写入线程。"""
        workers = []
        for task in self.tasks.values():
            if task.worker is not None:
                if task.status != "committing":
                    task.worker.cancel()
                workers.append(task.worker)
        await asyncio.shield(asyncio.gather(*workers, return_exceptions=True))
        self.tasks.clear()


qr_logins = QrLoginManager()
