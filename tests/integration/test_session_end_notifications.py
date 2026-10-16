"""会话结束通知不能将录制失败或尚未生成成品报告为交付成功。"""

from __future__ import annotations

from collections import deque

import pytest

from app.db.entities import AppSetting, LiveRoom, RecordingSession, SessionStatus
from app.db.session import get_session
from app.web.services import notifications, rooms


def _session(status: str) -> None:
    """建立无媒体会话，复现 FFmpeg 在首个片段前退出的情况。"""
    with get_session() as db:
        db.add(LiveRoom(id=1, input_url="1"))
        db.add(RecordingSession(id=1, room_id=1, status=status))


@pytest.mark.parametrize("upload_enabled", [False, True])
async def test_failed_session_never_notifies_success_or_opens_directory(
    temp_db: None, monkeypatch: pytest.MonkeyPatch, upload_enabled: bool
) -> None:
    """上传开关均不掩盖录制失败，空总结请求仍保留以供时间线收尾。"""
    _session(SessionStatus.ERROR)
    opened: list[str] = []
    monkeypatch.setattr(notifications, "_NOTIFICATIONS", deque(maxlen=200))
    monkeypatch.setattr(rooms.settings_store, "upload_active", lambda: upload_enabled)
    monkeypatch.setattr(rooms, "open_path", lambda path: opened.append(path) or True)

    await rooms._on_session_end(1)

    (event,) = notifications.get_notifications()
    assert event["kind"] == "error"
    assert "录制异常" in event["message"]
    assert "切片已保存" not in event["message"]
    assert "自动进入上传队列" not in event["message"]
    assert event["data"]["session_status"] == SessionStatus.ERROR
    assert event["data"]["directory_open_requested"] is False
    assert opened == []
    with get_session() as db:
        assert db.get(AppSetting, "session_timeline_summary_request:1") is not None


@pytest.mark.parametrize("upload_enabled", [False, True])
@pytest.mark.parametrize("open_result", [False, True])
async def test_stopped_session_reports_pending_outputs_and_real_open_result(
    temp_db: None, monkeypatch: pytest.MonkeyPatch, upload_enabled: bool, open_result: bool
) -> None:
    """结束通知不宣称空目录已有成品，并保留文件管理器请求的真实结果。"""
    _session(SessionStatus.STOPPED)
    opened: list[str] = []
    monkeypatch.setattr(notifications, "_NOTIFICATIONS", deque(maxlen=200))
    monkeypatch.setattr(rooms.settings_store, "upload_active", lambda: upload_enabled)
    monkeypatch.setattr(rooms, "open_path", lambda path: opened.append(path) or open_result)

    await rooms._on_session_end(1)

    (event,) = notifications.get_notifications()
    assert event["kind"] == "info"
    assert "切片已保存" not in event["message"]
    assert "自动进入上传队列" not in event["message"]
    assert "已弹出" not in event["message"]
    assert event["data"]["directory_open_requested"] is (open_result and not upload_enabled)
    assert len(opened) == (0 if upload_enabled else 1)
