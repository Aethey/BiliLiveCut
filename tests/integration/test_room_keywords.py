"""房间关键词从 Web 保存到热点分桶及候选评分的实际生效链。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.analysis.audio import AudioFeatures
from app.analysis.hotspot_detector import build_segment_signal_buckets
from app.analysis.keywords import match_keywords
from app.core.config import settings
from app.db.entities import LiveRoom, RawSegment, RecordingSession, Transcript
from app.db.session import get_session

if TYPE_CHECKING:
    from pytest import MonkeyPatch


def _seed_room(number: int) -> tuple[int, int]:
    start = datetime(2026, 10, 12, 12)
    text = "主播正在介绍星河试炼的活动规则。"
    with get_session() as db:
        room = LiveRoom(input_url=f"room-keywords:{number}", room_id=number)
        db.add(room)
        db.flush()
        assert room.id is not None
        recording = RecordingSession(room_id=room.id, started_at=start)
        db.add(recording)
        db.flush()
        assert recording.id is not None
        segment = RawSegment(
            session_id=recording.id,
            seq=0,
            file_path="room-keywords.ts",
            start_ts=start,
            end_ts=start + timedelta(seconds=30),
            duration_s=30,
        )
        db.add(segment)
        db.flush()
        assert segment.id is not None
        db.add(
            Transcript(
                segment_id=segment.id,
                final_text=text,
                words_json=json.dumps(
                    [{"w": text, "start": 1, "end": 8}],
                    ensure_ascii=False,
                ),
            )
        )
        return room.id, segment.id


def test_saved_keywords_affect_only_that_room_and_clear_on_next_analysis(
    temp_db: None, monkeypatch: MonkeyPatch
) -> None:
    from app.web.main import app

    monkeypatch.setattr(settings, "trend_enabled", False)
    room_id, segment_id = _seed_room(1001)
    _other_room_id, other_segment_id = _seed_room(1002)
    client = TestClient(app)
    response = client.patch(f"/api/rooms/{room_id}", json={"room_config": {"highlight_keywords": ["星河试炼"]}})
    assert response.status_code == 200, response.text
    buckets, _ = build_segment_signal_buckets(segment_id)
    other, _ = build_segment_signal_buckets(other_segment_id)
    assert max(bucket.asr_keyword_score or 0 for bucket in buckets) == pytest.approx(1 / 3)
    assert all((bucket.asr_keyword_score or 0) == 0 for bucket in other)
    assert client.patch(f"/api/rooms/{room_id}", json={"room_config": {"highlight_keywords": []}}).status_code == 200
    cleared, _ = build_segment_signal_buckets(segment_id)
    assert all((bucket.asr_keyword_score or 0) == 0 for bucket in cleared)


def test_room_keywords_reach_candidate_scoring(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    from app.analysis import llm
    from app.pipeline.workers.analyze import _score_segment_draft
    from app.web.main import app

    monkeypatch.setattr(settings, "trend_enabled", False)
    monkeypatch.setattr(settings, "highlight_init_threshold", 0)
    monkeypatch.setattr(llm, "judge_highlight", lambda *_args, **_kwargs: None)
    room_id, segment_id = _seed_room(1003)
    client = TestClient(app)
    assert (
        client.patch(f"/api/rooms/{room_id}", json={"highlight_threshold": 0.1, "review_threshold": 0.1}).status_code
        == 200
    )
    audio = AudioFeatures(
        sample_rate=16000,
        hop_s=1,
        times=np.array([1.0, 5.0, 8.0]),
        rms=np.array([0.2, 0.9, 0.2]),
        duration_s=30,
        silences=[],
    )
    before = _score_segment_draft(segment_id, audio_features=audio, peak_offset_s=5)
    assert (
        client.patch(f"/api/rooms/{room_id}", json={"room_config": {"highlight_keywords": ["星河试炼"]}}).status_code
        == 200
    )
    result = _score_segment_draft(segment_id, audio_features=audio, peak_offset_s=5)
    assert result is not None
    assert before is not None and result["rule_score"] > before["rule_score"]
    payload = json.loads(result["features_json"])
    assert payload["keyword_hits"] == ["星河试炼"]
    assert payload["features"]["keywords"] == pytest.approx(1 / 3)


def test_keyword_merge_is_case_insensitive_unique_and_keeps_global_terms() -> None:
    score, hits = match_keywords("ALPHA 五杀 星河试炼", ["Alpha", " alpha ", "星河试炼", "五杀"])
    assert hits.count("alpha") == hits.count("五杀") == 1
    assert "星河试炼" in hits
    assert score == 1
