"""整场分析的可核验输入覆盖率；空文本本身不是无语音证据。"""

from __future__ import annotations

import json
from typing import Any

from sqlmodel import Session, select

from app.analysis.transcription.content import transcript_text
from app.core.sanitize import sanitize_diagnostic
from app.db.entities import RawSegment, SegmentTask, Transcript


def session_asr_coverage(db: Session, session_id: int) -> dict[str, Any]:
    """读取每段转写证据及缺口，时间未知时明确保留 None。"""
    segments = db.exec(
        select(RawSegment).where(RawSegment.session_id == session_id).order_by(RawSegment.seq, RawSegment.id)
    ).all()
    ids = [segment.id for segment in segments]
    transcripts = db.exec(select(Transcript).where(Transcript.segment_id.in_(ids))).all() if ids else []
    by_segment = {item.segment_id: item for item in transcripts}
    tasks = db.exec(select(SegmentTask).where(SegmentTask.session_id == session_id).order_by(SegmentTask.id)).all()
    by_task = {task.segment_id: task for task in tasks}
    missing: list[dict[str, Any]] = []
    speech = no_speech = 0
    known_duration = covered_duration = 0.0
    unknown_durations = 0
    for segment in segments:
        transcript = by_segment.get(segment.id)
        task = by_task.get(segment.id)
        try:
            auxiliary = json.loads(transcript.auxiliary_json or "{}") if transcript else {}
        except (ValueError, TypeError):
            auxiliary = {}
        result_type = auxiliary.get("result_type") if isinstance(auxiliary, dict) else None
        covered = False
        if transcript is not None and transcript_text(transcript).strip():
            speech += 1
            covered = True
        elif result_type == "no_speech":
            no_speech += 1
            covered = True
        else:
            missing.append(
                {
                    "segment_id": segment.id,
                    "sequence": segment.seq,
                    "start_at": segment.start_ts.isoformat() if segment.start_ts else None,
                    "end_at": segment.end_ts.isoformat() if segment.end_ts else None,
                    "stage": task.stage if task else None,
                    "reason": sanitize_diagnostic(task.last_error, limit=300)
                    if task and task.last_error
                    else "缺少有效转写或无语音证据",
                }
            )
        if segment.duration_s is None:
            unknown_durations += 1
        else:
            known_duration += segment.duration_s
            if covered:
                covered_duration += segment.duration_s
    total = len(segments)
    return {
        "mode": "degraded" if missing else "full",
        "total_segments": total,
        "speech_segments": speech,
        "no_speech_segments": no_speech,
        "covered_segments": speech + no_speech,
        "coverage_ratio": (speech + no_speech) / total if total else None,
        "known_duration_s": known_duration,
        "covered_duration_s": covered_duration,
        "unknown_duration_segments": unknown_durations,
        "missing": missing,
    }
