"""弹幕烧录的场次隔离、时间轴、资源上限和 ASS 文本安全。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError

from app.clipping.danmaku import write_danmaku_ass
from app.clipping.models import ClipOptions
from app.core.config import Settings
from app.db.entities import AppSetting, Danmaku, DanmakuType
from app.db.session import get_session
from app.plugins.live_source import DanmakuStatus
from app.recording.danmaku import DanmakuEvidence
from app.recording.import_comments import preprocess_comments

_BASE = datetime(2026, 10, 12, tzinfo=UTC)


def _event(at: float, text: str | None, *, session: int = 1, kind: str = DanmakuType.DANMAKU) -> Danmaku:
    return Danmaku(session_id=session, room_id=1, ts=_BASE + timedelta(seconds=at), content=text, msg_type=kind)


def test_frozen_lag_and_clip_window_preserve_existing_scroll_position(temp_db: None, tmp_path: Path) -> None:
    with get_session() as db:
        evidence = DanmakuEvidence(status=DanmakuStatus.AVAILABLE, lag_s=2)
        db.add(AppSetting(key="session_danmaku:1", value=evidence.model_dump_json()))
        db.add_all(
            [
                _event(10, "already moving"),
                _event(13, "inside"),
                _event(8, "expired"),
                _event(17, "at end"),
                _event(12, "other session", session=2),
                _event(12, "gift", kind=DanmakuType.GIFT),
                _event(12, "paid", kind=DanmakuType.SUPERCHAT),
                _event(12, None),
            ]
        )
    path = write_danmaku_ass(
        1,
        _BASE + timedelta(seconds=10),
        5,
        1920,
        1080,
        ClipOptions(danmaku=True, danmaku_duration_s=4),
        tmp_path / "clip.ass",
    )
    assert path is not None
    text = path.read_text(encoding="utf-8")
    events = [line for line in text.splitlines() if line.startswith("Dialogue:")]
    assert len(events) == 2
    assert "0:00:00.00,0:00:02.00" in events[0] and "already moving" in events[0]
    assert "\\move(690.00," in events[0]  # 已经滚动两秒，不能重新从画面右侧开始。
    assert "0:00:01.00,0:00:05.00" in events[1] and "inside" in events[1]
    assert all(value not in text for value in ("expired", "at end", "other session", "gift", "paid"))


def test_ass_control_sequences_and_line_breaks_remain_plain_text(temp_db: None, tmp_path: Path) -> None:
    with get_session() as db:
        db.add(_event(0, "{\\p1}m 0 0 l 99 99\\N\nDialogue: 9,ATTACK\x00\u202e"))
        db.add(_event(0, "长" * 200))
    path = write_danmaku_ass(1, _BASE, 3, 1920, 1080, ClipOptions(danmaku=True), tmp_path / "safe.ass")
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "｛＼p1｝" in text and "＼N Dialogue: 9,ATTACK" in text
    assert "\\p1" not in text and "\x00" not in text and "\u202e" not in text
    assert sum(line.startswith("Dialogue:") for line in text.splitlines()) == 2
    assert "长" * 119 + "…" in text and "长" * 120 not in text


@pytest.mark.parametrize("mode", ["scroll", "top", "bottom"])
def test_full_lanes_drop_new_events_until_previous_event_leaves(
    temp_db: None, tmp_path: Path, mode: Literal["scroll", "top", "bottom"]
) -> None:
    with get_session() as db:
        db.add_all([_event(0, "first"), _event(1, "overlap"), _event(2, "next")])
    options = ClipOptions(danmaku=True, danmaku_mode=mode, danmaku_max_lines=1, danmaku_duration_s=2)
    path = write_danmaku_ass(1, _BASE, 4, 1920, 1080, options, tmp_path / "dense.ass")
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "first" in text and "next" in text and "overlap" not in text
    assert "0:00:02.00,0:00:04.00" in text


def test_input_limit_is_visible_and_color_opacity_are_encoded(
    temp_db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.clipping.danmaku as module

    monkeypatch.setattr(module, "_MAX_EVENTS", 2)
    with get_session() as db:
        db.add_all([_event(0, "first"), _event(2, "second"), _event(4, "beyond limit")])
    notices: list[str] = []
    sink = module.logger.add(lambda message: notices.append(str(message)))
    try:
        path = write_danmaku_ass(
            1,
            _BASE,
            8,
            1920,
            1080,
            ClipOptions(danmaku=True, danmaku_color="fF8000", danmaku_opacity=0.5),
            tmp_path / "bounded.ass",
        )
    finally:
        module.logger.remove(sink)
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "&H800080FF" in text and "beyond limit" not in text
    assert any("session=1" in notice and "超过 2 条" in notice for notice in notices)


def test_empty_or_disabled_overlay_does_not_create_ass(temp_db: None, tmp_path: Path) -> None:
    output = tmp_path / "absent.ass"
    for options in (ClipOptions(), ClipOptions(danmaku=True), ClipOptions(danmaku=True, danmaku_opacity=0)):
        assert write_danmaku_ass(1, _BASE, 3, 320, 240, options, output) is None
        assert not output.exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("mode", "injected"),
        ("font_size", 11),
        ("font_size", 121),
        ("max_lines", 0),
        ("max_lines", 21),
        ("color", "FFFFFF\nStyle: evil"),
        ("color", "#FFFFFF"),
        ("opacity", -0.1),
        ("opacity", 1.1),
        ("opacity", float("nan")),
        ("duration_s", float("inf")),
        ("duration_s", 1),
        ("duration_s", 21),
    ],
)
def test_settings_and_direct_calls_reject_unsafe_style(field: str, value: str | int | float, tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{f"clip_danmaku_{field}": value})
    options = ClipOptions(danmaku=True)
    setattr(options, f"danmaku_{field}", value)
    with pytest.raises(ValueError):
        write_danmaku_ass(1, _BASE, 3, 320, 240, options, tmp_path / "rejected.ass")
    assert not (tmp_path / "rejected.ass").exists()


@pytest.mark.parametrize("width,height,duration", [(0, 240, 3), (320, 0, 3), (16385, 240, 3), (320, 240, float("nan"))])
def test_unknown_dimensions_or_invalid_duration_fail_explicitly(
    width: int, height: int, duration: float, tmp_path: Path
) -> None:
    with pytest.raises(ValueError):
        write_danmaku_ass(1, _BASE, duration, width, height, ClipOptions(danmaku=True), tmp_path / "bad.ass")


def test_local_import_uses_media_time_without_live_delay(temp_db: None, tmp_path: Path) -> None:
    source = tmp_path / "local.json"
    source.write_text('[{"offset_s":1.25,"text":"local comment"}]', encoding="utf-8")
    report = preprocess_comments(source, 10)
    with get_session() as db:
        db.add_all([_event(event.offset_s, event.text) for event in report.events])
    path = write_danmaku_ass(1, _BASE, 5, 320, 240, ClipOptions(danmaku=True), tmp_path / "local.ass")
    assert path is not None
    assert "0:00:01.25,0:00:05.00" in path.read_text(encoding="utf-8")
