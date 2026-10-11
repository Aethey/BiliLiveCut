"""终态租约、取消重试及全场分析的真实数据库回归。"""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlmodel import select

from app.analysis import session_summary
from app.db.entities import AppSetting, LiveRoom, RawSegment, RecordingSession, SegmentTask, TaskStatus, Transcript
from app.db.session import get_session
from app.pipeline import stale_recovery, task_worker
from app.pipeline.stage_result import mark_failed


def seed(stage: str = TaskStatus.TRANSCRIBING) -> tuple[int, int, int]:
    with get_session() as db:
        room = LiveRoom(input_url="recovery")
        db.add(room)
        db.flush()
        recording = RecordingSession(room_id=room.id, ended_at=datetime.now(UTC))
        db.add(recording)
        db.flush()
        segment = RawSegment(session_id=recording.id, seq=0, file_path="part.wav", duration_s=8.336)
        db.add(segment)
        db.flush()
        task = SegmentTask(
            segment_id=segment.id,
            session_id=recording.id,
            stage=stage,
            claimed_by="worker",
            lease_token="old",
            claimed_at=datetime.now(UTC),
        )
        db.add(task)
        db.flush()
        return task.id, segment.id, recording.id


@pytest.mark.parametrize("permanent", [True, False])
def test_failure_releases_all_lease_fields(temp_db: None, permanent: bool) -> None:
    task_id, _, _ = seed()
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        mark_failed(task, "model failed", permanent=permanent)
        db.add(task)
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        assert task.claimed_by is task.claimed_at is task.lease_token is task.heartbeat_at is None
        assert task.failed_stage == TaskStatus.TRANSCRIBING
        assert task.last_error == "model failed"


@pytest.mark.parametrize("stage", [TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.COMPLETED])
def test_terminal_cancel_returns_business_failure(temp_db: None, stage: str) -> None:
    task_id, _, _ = seed(stage)
    assert task_worker.cancel_task(task_id) is False


@pytest.mark.parametrize(
    "stage,expected",
    [
        (TaskStatus.ANALYZING, TaskStatus.QUEUED_FOR_ANALYSIS),
        (TaskStatus.RENDERING, TaskStatus.QUEUED_FOR_RENDER),
        (TaskStatus.QUEUED_FOR_PUBLISH, TaskStatus.QUEUED_FOR_PUBLISH),
    ],
)
def test_cancel_then_retry_uses_current_stage_not_old_failure(temp_db: None, stage: str, expected: str) -> None:
    task_id, _, session_id = seed(stage)
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        task.failed_stage = TaskStatus.TRANSCRIBING
        db.add(task)
    assert task_worker.cancel_task(task_id)
    with get_session() as db:
        old_request = json.loads(db.get(AppSetting, f"session_timeline_summary_request:{session_id}").value)
    assert task_worker.retry_task(task_id)
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        assert task.stage == expected
        assert task.stage_key.endswith(expected)
        assert task.lease_token is task.claimed_by is task.completed_at is None
        new_request = json.loads(db.get(AppSetting, f"session_timeline_summary_request:{session_id}").value)
        assert new_request["request_id"] != old_request["request_id"]


@pytest.mark.parametrize("expired", [True, False])
def test_null_heartbeat_uses_claim_time(temp_db: None, monkeypatch: pytest.MonkeyPatch, expired: bool) -> None:
    task_id, _, _ = seed()
    now = datetime.now(UTC)
    monkeypatch.setattr(stale_recovery, "now_utc", lambda: now)
    monkeypatch.setattr(stale_recovery.settings, "stale_timeout_s", 120)
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        task.claimed_at = now - timedelta(seconds=180 if expired else 10)
        db.add(task)
    stale_recovery.recover_stale()
    with get_session() as db:
        assert db.get(SegmentTask, task_id).stage == (
            TaskStatus.QUEUED_FOR_TRANS if expired else TaskStatus.TRANSCRIBING
        )


def test_failed_lease_cleanup_unblocks_degraded_summary(temp_db: None) -> None:
    _, _, session_id = seed(TaskStatus.FAILED)
    session_summary.request_session_timeline_summary(session_id, reason="ended")
    assert session_summary.claim_pending_session_summary() is None
    stale_recovery.recover_stale()
    claim = session_summary.claim_pending_session_summary()
    assert claim is not None and session_summary.execute_session_summary_claim(claim)
    view = session_summary.session_timeline_summary_view(session_id, processing_state="completed", ended=True)
    assert view["status"] == "ready"
    assert view["coverage"]["mode"] == "degraded"
    assert len(view["coverage"]["missing"]) == 1


def test_no_speech_is_covered_without_sending_empty_text_to_llm(temp_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _, segment_id, session_id = seed(TaskStatus.COMPLETED)
    stale_recovery.recover_stale()
    from app.analysis import llm

    def forbidden(*args: object, **kwargs: object) -> str:
        pytest.fail("无文本不得调用 LLM")

    monkeypatch.setattr(llm, "call_text", forbidden)
    with get_session() as db:
        db.add(
            Transcript(segment_id=segment_id, final_text="", auxiliary_json=json.dumps({"result_type": "no_speech"}))
        )
    result = session_summary.build_session_timeline_summary(session_id)
    assert result["coverage"]["mode"] == "full"
    assert result["coverage"]["covered_duration_s"] == 8.336
    assert result["coverage"]["no_speech_segments"] == 1
    from app.web.services.transcripts import list_transcripts

    item = list_transcripts(session_id=session_id)[0]
    assert item["result_type"] == "no_speech" and item["text"] == ""


def test_changed_input_during_generation_is_requeued(temp_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _, segment_id, session_id = seed(TaskStatus.COMPLETED)
    stale_recovery.recover_stale()
    session_summary.request_session_timeline_summary(session_id, reason="ended")
    claim = session_summary.claim_pending_session_summary()
    original = session_summary.build_session_timeline_summary

    def change_input(recording_id: int) -> dict[str, object]:
        result = original(recording_id)
        with get_session() as db:
            db.add(Transcript(segment_id=segment_id, final_text="晚到的转写"))
        return result

    monkeypatch.setattr(session_summary, "build_session_timeline_summary", change_input)
    assert session_summary.execute_session_summary_claim(claim) is False
    with get_session() as db:
        assert db.get(AppSetting, f"session_timeline_summary:{session_id}") is None
        request = json.loads(db.get(AppSetting, f"session_timeline_summary_request:{session_id}").value)
        assert request["status"] == "pending"
        assert request["attempts"] == 0


def test_compensation_is_independent_of_timeline_and_idempotent(temp_db: None) -> None:
    _, _, session_id = seed(TaskStatus.COMPLETED)
    stale_recovery.recover_stale()
    assert session_summary.compensate_session_summaries() == (1, 0)
    assert session_summary.compensate_session_summaries() == (0, 0)
    claim = session_summary.claim_pending_session_summary()
    assert claim.session_id == session_id
    assert session_summary.execute_session_summary_claim(claim)
    assert session_summary.compensate_session_summaries() == (0, 0)


def test_shutdown_revokes_lease_and_rejects_late_commit_without_attempt(temp_db: None) -> None:
    from app.pipeline.lease import TaskLease
    from app.pipeline.workers.transcribe import commit_transcript

    task_id, segment_id, _ = seed()
    assert stale_recovery.release_stopped_computations("worker") == 1
    commit_transcript(
        TaskLease(task_id, "worker", "old", TaskStatus.TRANSCRIBING), {"text": "late", "segment_id": segment_id}, 20
    )
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        assert task.stage == TaskStatus.QUEUED_FOR_TRANS
        assert task.attempts == 0 and task.lease_token is None
        assert db.exec(select(Transcript)).all() == []


def test_summary_stop_fences_old_result_without_attempt(temp_db: None) -> None:
    _, _, session_id = seed(TaskStatus.COMPLETED)
    stale_recovery.recover_stale()
    session_summary.request_session_timeline_summary(session_id, reason="ended")
    claim = session_summary.claim_pending_session_summary()
    result = session_summary.build_session_timeline_summary(session_id)
    session_summary.interrupt_summary_execution(claim)
    assert session_summary._commit_summary_result(claim, result) is False
    with get_session() as db:
        request = json.loads(db.get(AppSetting, f"session_timeline_summary_request:{session_id}").value)
        assert request["attempts"] == 0
        assert request["request_id"] != claim.request_id


def test_filled_gap_replaces_degraded_summary_as_recovered(temp_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.analysis import llm

    task_id, segment_id, session_id = seed(TaskStatus.FAILED)
    stale_recovery.recover_stale()
    session_summary.request_session_timeline_summary(session_id, reason="ended")
    assert session_summary.execute_session_summary_claim(session_summary.claim_pending_session_summary())
    assert task_worker.retry_task(task_id)
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        task.stage = TaskStatus.COMPLETED
        db.add(task)
        db.add(Transcript(segment_id=segment_id, final_text="恢复后的内容"))
    monkeypatch.setattr(llm, "call_text", lambda *args, **kwargs: '{"summary":"补齐结果"}')
    assert session_summary.execute_session_summary_claim(session_summary.claim_pending_session_summary())
    view = session_summary.session_timeline_summary_view(session_id, ended=True, processing_state="completed")
    assert view["summary_mode"] == "recovered"
    assert view["coverage"]["coverage_ratio"] == 1
    assert view["summary"] == "补齐结果"


def test_resource_reservations_are_released_by_lease_generation(temp_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import resource_budget
    from app.pipeline import lifecycle, scheduler

    reservations = {(159, "old"): {"cpu": 1}, (159, "new"): {"cpu": 2}}
    released: list[dict[str, int]] = []
    monkeypatch.setattr(lifecycle, "_task_resources", reservations)
    monkeypatch.setattr(resource_budget, "release_resources", lambda **cost: released.append(cost))
    monkeypatch.setattr(scheduler, "start_heartbeat_thread", lambda *args: threading.Event())
    monkeypatch.setattr(scheduler, "clear_heartbeat_if_own", lambda *args: None)
    monkeypatch.setattr(scheduler, "run_transcribe", lambda lease: None)
    scheduler.execute_task(159, TaskStatus.TRANSCRIBING, "old")
    assert reservations == {(159, "new"): {"cpu": 2}}
    assert released == [{"cpu": 1}]
    scheduler.execute_task(159, TaskStatus.TRANSCRIBING, "new")
    assert reservations == {}
    assert released == [{"cpu": 1}, {"cpu": 2}]


async def test_shutdown_failure_still_fences_computation_and_summary(
    temp_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.analysis.transcription import process_pool

    task_id, _, _ = seed()
    _, _, session_id = seed(TaskStatus.COMPLETED)
    stale_recovery.recover_stale()
    session_summary.request_session_timeline_summary(session_id, reason="ended")
    claim = session_summary.claim_pending_session_summary()
    result = session_summary.build_session_timeline_summary(session_id)
    monkeypatch.setattr(task_worker, "_WORKER_ID", "worker")

    def broken_shutdown(grace: float) -> None:
        raise RuntimeError("native process exit unconfirmed")

    monkeypatch.setattr(process_pool, "get_process_pool", lambda: SimpleNamespace(shutdown=broken_shutdown))
    worker = task_worker.TaskWorker()
    dummy = asyncio.create_task(asyncio.sleep(0))
    worker._summary_claims[dummy] = claim
    with pytest.raises(RuntimeError, match="native process exit unconfirmed"):
        await worker.stop()
    await dummy
    with get_session() as db:
        assert db.get(SegmentTask, task_id).lease_token is None
    assert session_summary._commit_summary_result(claim, result) is False


def test_shutdown_does_not_consume_task_retry_budget(temp_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.analysis.transcription.process_pool import ASRShutdownError
    from app.pipeline import scheduler

    task_id, _, _ = seed()
    monkeypatch.setattr(scheduler, "_WORKER_ID", "worker")
    monkeypatch.setattr(scheduler, "start_heartbeat_thread", lambda *args: threading.Event())

    def interrupted(lease: object) -> None:
        raise ASRShutdownError("shutdown deadline")

    monkeypatch.setattr(scheduler, "run_transcribe", interrupted)
    scheduler.execute_task(task_id, TaskStatus.TRANSCRIBING, "old")
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        assert task.stage == TaskStatus.QUEUED_FOR_TRANS and task.attempts == 0
        assert task.lease_token is None
