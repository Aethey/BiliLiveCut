"""网页扫码任务：二维码留在内存，后台轮询并保存账号 Cookie。"""

from __future__ import annotations

import asyncio
import base64
import io
import secrets
import time
from dataclasses import dataclass, field

import httpx

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


class QrLoginManager:
    """限制并发登录、保留终态供查询，并在服务关闭时取消轮询。"""

    retention_seconds = 600
    poll_interval = 1.5
    max_tasks = 32

    def __init__(self) -> None:
        self.tasks: dict[str, LoginTask] = {}

    def start(self, owner: str) -> str:
        """创建任务；同一发起者只能保留一个活动登录。"""
        for token, task in list(self.tasks.items()):
            if (
                task.worker is not None
                and task.worker.done()
                and time.monotonic() - task.created > self.retention_seconds
            ):
                del self.tasks[token]
        if any(t.owner == owner and t.worker is not None and not t.worker.done() for t in self.tasks.values()):
            raise QrLoginError("已有扫码登录正在进行，请先完成或取消。")
        if len(self.tasks) >= self.max_tasks:
            raise QrLoginError("登录任务过多，请稍后重试。")
        token = secrets.token_urlsafe(24)
        task = LoginTask(owner)
        self.tasks[token] = task
        task.worker = asyncio.create_task(self._run(task))
        return token

    def get(self, token: str, owner: str) -> LoginTask | None:
        """其他发起者和未知 ID 均不能读取任务。"""
        task = self.tasks.get(token)
        return task if task is not None and task.owner == owner else None

    @staticmethod
    def _render(url: str) -> str:
        import qrcode
        from qrcode.image.svg import SvgPathImage

        output = io.BytesIO()
        qrcode.make(url, image_factory=SvgPathImage).save(output)
        return "data:image/svg+xml;base64," + base64.b64encode(output.getvalue()).decode("ascii")

    async def _run(self, task: LoginTask) -> None:
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                login = WebQrLogin(client)
                await login.generate_qrcode()
                task.image = await asyncio.to_thread(self._render, login.url)
                task.status = QrState.SCAN
                while time.monotonic() - task.created < self.retention_seconds:
                    state = await login.check_state()
                    if state == QrState.DONE:
                        from app.web.login_handler import _save_cookie

                        cookie = "; ".join(f"{key}={value}" for key, value in login.get_credential().items())
                        _save_cookie(cookie)
                    task.status = state
                    if state in (QrState.DONE, QrState.TIMEOUT):
                        task.image = ""
                        return
                    await asyncio.sleep(self.poll_interval)
                task.status = "error"
                task.error = "登录任务已结束，请重新申请二维码。"
        except asyncio.CancelledError:
            task.status = "cancelled"
            raise
        except Exception:
            # 不公开异常链：远端 URL、解析结果可能包含二维码密钥和 Cookie。
            task.status = "error"
            task.error = "扫码登录失败，请检查网络和运行环境后重试。"
        finally:
            if task.status not in (QrState.SCAN, QrState.CONF):
                task.image = ""

    async def cancel(self, token: str, owner: str) -> bool:
        """取消轮询并等待客户端关闭，完成的 Cookie 不会被清除。"""
        task = self.get(token, owner)
        if task is None:
            return False
        if task.worker is not None and not task.worker.done():
            task.worker.cancel()
            await asyncio.gather(task.worker, return_exceptions=True)
            task.status = "cancelled"
            task.image = ""
        return True

    async def stop(self) -> None:
        """关闭全部异步任务和 HTTP 客户端，不留后台登录进程。"""
        workers = [task.worker for task in self.tasks.values() if task.worker is not None]
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        self.tasks.clear()


qr_logins = QrLoginManager()
