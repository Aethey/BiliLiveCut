"""渲染提交的成片关联、租约和人工审核回归。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import select

from app.db.entities import (
    ClipVariant,
    FinalClip,
    HighlightCandidate,
    HighlightEvent,
    RawSegment,
    ReviewStatus,
    SegmentTask,
    TaskStatus,
)
from app.db.session import get_session
from app.pipeline.lease import TaskLease
from app.pipeline.workers import render


def seed_render(tmp_path: Path) -> tuple[TaskLease, dict[str, Any]]:
    """建立真实来源链，并让变体与成片主键刻意错开。"""
    now = datetime.now(UTC)
    temp = tmp_path / "lease.partial.mp4"
    temp.write_bytes(b"rendered media")
    with get_session() as db:
        unrelated = FinalClip(candidate_id=999, file_path=str(tmp_path / "other.mp4"))
        db.add(unrelated)
        segment = RawSegment(session_id=1, seq=0, file_path=str(tmp_path / "raw.ts"))
        candidate = HighlightCandidate(
            session_id=1, peak_ts=now, start_ts=now, end_ts=now + timedelta(seconds=20), dedup_hash="render"
        )
        db.add(segment)
        db.add(candidate)
        db.flush()
        event = HighlightEvent(
            candidate_id=candidate.id, session_id=1, segment_id=segment.id, review_status=ReviewStatus.APPROVED_SOLO
        )
        db.add(event)
        db.flush()
        task = SegmentTask(
            segment_id=segment.id,
            session_id=1,
            candidate_id=candidate.id,
            event_id=event.id,
            stage=TaskStatus.RENDERING,
            claimed_by="worker",
            lease_token="lease",
        )
        db.add(task)
        db.flush()
        lease = TaskLease(task.id, "worker", "lease", TaskStatus.RENDERING)
        result = dict(
            temp_path=str(temp),
            formal_path=str(tmp_path / "final.mp4"),
            event_id=event.id,
            candidate_id=candidate.id,
            render_config_hash="config",
            content_hash="content",
            duration_s=20,
        )
    return lease, result


def test_render_and_reuse_reference_actual_final_clip(temp_db: None, tmp_path: Path) -> None:
    lease, result = seed_render(tmp_path)
    render.commit_render(lease, result, 1)
    with get_session() as db:
        task = db.get(SegmentTask, lease.task_id)
        clip = db.get(FinalClip, task.clip_id)
        variant = db.exec(select(ClipVariant)).one()
        assert clip.id != variant.id and clip.candidate_id == task.candidate_id
        assert Path(clip.file_path).read_bytes() == b"rendered media"
        task.stage = TaskStatus.RENDERING
        task.claimed_by = lease.worker_id
        task.lease_token = lease.lease_token
        db.add(task)
        clip_id = clip.id
    Path(result["temp_path"]).write_bytes(b"unused retry")
    render.commit_render(lease, result, 2)
    with get_session() as db:
        assert db.get(SegmentTask, lease.task_id).clip_id == clip_id
        assert len(db.exec(select(FinalClip)).all()) == 2


@pytest.mark.parametrize("reject", [False, True])
def test_cancel_or_reject_during_move_cannot_be_overwritten(
    temp_db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reject: bool
) -> None:
    lease, result = seed_render(tmp_path)
    replace = Path.replace

    def cancel_after_move(path: Path, target: str | Path) -> Path:
        moved = replace(path, target)
        if reject:
            from app.pipeline.rejection import reject_candidate_and_outputs

            with get_session() as db:
                reject_candidate_and_outputs(db, result["candidate_id"], rejected_by="test")
        else:
            from app.pipeline.task_worker import cancel_task

            assert cancel_task(lease.task_id)
        return moved

    monkeypatch.setattr(Path, "replace", cancel_after_move)
    render.commit_render(lease, result, 1)
    with get_session() as db:
        assert db.get(SegmentTask, lease.task_id).stage == TaskStatus.CANCELLING
        assert db.exec(select(FinalClip).where(FinalClip.candidate_id == result["candidate_id"])).first() is None
        if reject:
            assert db.get(HighlightCandidate, result["candidate_id"]).status == "rejected"
    assert not list(tmp_path.glob("final*.mp4"))


def test_wrong_clip_cannot_prepare_upload(temp_db: None, tmp_path: Path) -> None:
    from app.pipeline.workers.publish import prepare_publish_attempt

    lease, _ = seed_render(tmp_path)
    with get_session() as db:
        task = db.get(SegmentTask, lease.task_id)
        task.stage = TaskStatus.PUBLISHING
        task.clip_id = 1
        db.add(task)
    prepared = prepare_publish_attempt(TaskLease(lease.task_id, "worker", "lease", TaskStatus.PUBLISHING))
    assert "来源不一致" in prepared["error"] and not prepared.get("ready")


def test_recovered_variant_creates_clip_and_updates_candidate(temp_db: None, tmp_path: Path) -> None:
    lease, result = seed_render(tmp_path)
    media = tmp_path / "recovered.mp4"
    media.write_bytes(b"recovered")
    with get_session() as db:
        db.add(
            ClipVariant(
                event_id=result["event_id"],
                render_config_hash="config",
                file_path=str(media),
                render_status="done",
                duration_s=20,
            )
        )
    render.commit_render(lease, result, 1)
    with get_session() as db:
        assert db.get(HighlightCandidate, result["candidate_id"]).status == "clipped"
        task = db.get(SegmentTask, lease.task_id)
        assert db.get(FinalClip, task.clip_id).file_path == str(media)


def test_stale_generation_does_not_remove_new_worker_output(
    temp_db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease, result = seed_render(tmp_path)
    replace = Path.replace
    new_result = {**result, "temp_path": str(tmp_path / "new.partial.mp4")}
    Path(new_result["temp_path"]).write_bytes(b"new worker media")

    def transfer_lease(path: Path, target: str | Path) -> Path:
        moved = replace(path, target)
        if str(path) == result["temp_path"]:
            with get_session() as db:
                task = db.get(SegmentTask, lease.task_id)
                task.lease_token = "new-lease"
                db.add(task)
            render.commit_render(TaskLease(lease.task_id, "worker", "new-lease", TaskStatus.RENDERING), new_result, 1)
        return moved

    monkeypatch.setattr(Path, "replace", transfer_lease)
    render.commit_render(lease, result, 2)
    with get_session() as db:
        task = db.get(SegmentTask, lease.task_id)
        assert task.stage == TaskStatus.RENDERED
        assert Path(db.get(FinalClip, task.clip_id).file_path).read_bytes() == b"new worker media"
    assert len(list(tmp_path.glob("final*.mp4"))) == 1
