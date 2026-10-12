"""Subtitle regressions cover transcript fallbacks and real FFmpeg pixels."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import select

from app.clipping.core import (
    _build_srt,
    _render_single_variant,
    _run_ffmpeg_clip,
    _write_concat_list,
    produce_clip,
    render_clip_to_file,
)
from app.clipping.models import ClipOptions
from app.core.config import settings
from app.db.entities import (
    ClipVariant,
    ClipVariantType,
    FinalClip,
    HighlightCandidate,
    HighlightEvent,
    IntroTemplate,
    RawSegment,
    RecordingSession,
    Transcript,
)
from app.db.session import get_session

_HAS_FFMPEG = shutil.which(settings.ffmpeg_path) is not None


def test_dense_estimated_subtitles_do_not_overlap_or_extend_past_clip(temp_db: None) -> None:
    segment = RawSegment(id=7, session_id=1, seq=0, file_path="segment.ts", duration_s=2)
    with get_session() as db:
        db.add(Transcript(segment_id=7, final_text="字" * 56, words_json="[]"))
    srt = _build_srt([segment], 0, 2)
    timings = [block.splitlines()[1] for block in srt.strip().split("\n\n")]
    assert timings == [
        "00:00:00,000 --> 00:00:00,500",
        "00:00:00,500 --> 00:00:01,000",
        "00:00:01,000 --> 00:00:01,500",
        "00:00:01,500 --> 00:00:02,000",
    ]


def test_text_only_transcript_generates_subtitles_for_the_selected_window(temp_db: None) -> None:
    segment = RawSegment(id=7, session_id=1, seq=6, file_path="segment.ts", duration_s=12)
    with get_session() as db:
        db.add(Transcript(segment_id=7, final_text="跳过跳过跳过保留保留保留", words_json="[]"))
    srt = _build_srt([segment], 6, 6)
    assert "保留保留保留" in srt and "跳过" not in srt
    assert "00:00:00,000 -->" in srt


def test_precise_silent_window_does_not_fall_back_to_full_text(temp_db: None) -> None:
    segment = RawSegment(id=7, session_id=1, seq=0, file_path="segment.ts", duration_s=12)
    with get_session() as db:
        db.add(
            Transcript(
                segment_id=7,
                final_text="前面讲话",
                words_json=json.dumps([{"w": "前面讲话", "start": 0, "end": 2}]),
            )
        )
    assert _build_srt([segment], 6, 6) == ""


def test_cross_segment_subtitles_keep_local_timestamps(temp_db: None) -> None:
    segments = [
        RawSegment(id=7, session_id=1, seq=6, file_path="first.ts", duration_s=10),
        RawSegment(id=8, session_id=1, seq=7, file_path="second.ts", duration_s=10),
    ]
    with get_session() as db:
        for segment_id, start, end, content in [(7, 8, 9, "第一段"), (8, 1, 2, "第二段")]:
            db.add(
                Transcript(
                    segment_id=segment_id,
                    final_text=content,
                    words_json=json.dumps([{"w": content, "start": start, "end": end}]),
                )
            )
    srt = _build_srt(segments, 8, 5)
    assert "第一段" in srt and "第二段" in srt
    assert "00:00:00,000 --> 00:00:01,000" in srt
    assert "00:00:03,000 --> 00:00:04,000" in srt


def _make_black_ts(path: Path, duration: int = 3) -> None:
    subprocess.run(
        [
            settings.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=black:size=320x240:rate=15:duration={duration}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={duration}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            "-f",
            "mpegts",
            str(path),
        ],
        capture_output=True,
        check=True,
        timeout=30,
    )


def _bright_pixels(path: Path, at_s: float) -> int:
    pixels = subprocess.run(
        [
            settings.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            str(at_s),
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-pix_fmt",
            "gray",
            "-f",
            "rawvideo",
            "-",
        ],
        capture_output=True,
        check=True,
        timeout=30,
    ).stdout
    assert len(pixels) == 320 * 240
    return sum(value > 150 for value in pixels)


@pytest.mark.parametrize("variant", [False, True])
@pytest.mark.skipif(not _HAS_FFMPEG, reason="需要 FFmpeg")
def test_burn_in_survives_nonzero_cross_segment_cut_and_special_path(
    temp_db: None, tmp_path: Path, variant: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "clip_loudnorm", False)
    monkeypatch.setattr(settings, "clip_remove_silence", False)
    special = tmp_path / "中文 it's [clip],semi;"
    special.mkdir()
    segments = []
    for index in range(2):
        source = special / f"source{index}.ts"
        _make_black_ts(source)
        segments.append(RawSegment(session_id=1, seq=index, file_path=str(source), duration_s=3))
    concat = _write_concat_list(segments, special)
    subtitle = special / "字幕's.srt"
    subtitle.write_text(
        "1\n00:00:00,100 --> 00:00:00,700\n中文 HELLO\n\n2\n00:00:01,300 --> 00:00:02,400\n跨段 WORLD\n",
        encoding="utf-8",
    )
    output = special / "result.mp4"
    if variant:
        with get_session() as db:
            candidate = HighlightCandidate(
                session_id=1,
                peak_ts=datetime.now(UTC),
                start_ts=datetime.now(UTC),
                end_ts=datetime.now(UTC),
                dedup_hash="subtitle-variant",
            )
            db.add(candidate)
            db.flush()
            event = HighlightEvent(candidate_id=candidate.id, session_id=1)
            db.add(event)
            db.flush()
            db.add(ClipVariant(event_id=event.id, variant_type=ClipVariantType.SUBTITLED))
            clip = FinalClip(candidate_id=candidate.id, file_path=str(output))
        monkeypatch.setattr(settings, "clip_vertical", True)
        _render_single_variant(
            concat,
            output,
            2,
            3,
            crf=20,
            preset="ultrafast",
            audio_bitrate="160k",
            subtitle=True,
            srt_path=subtitle,
            variant_type=ClipVariantType.SUBTITLED,
            clip=clip,
            options=ClipOptions(loudnorm=False, vertical=False, subtitle=True, preset="ultrafast"),
        )
    else:
        _run_ffmpeg_clip(
            concat,
            output,
            2,
            3,
            ClipOptions(loudnorm=False, subtitle=True, preset="ultrafast"),
            subtitle,
        )
    assert _bright_pixels(output, 0.3) > 20
    assert _bright_pixels(output, 0.9) == 0
    assert _bright_pixels(output, 1.6) > 20


@pytest.mark.skipif(not _HAS_FFMPEG, reason="需要 FFmpeg")
@pytest.mark.parametrize(
    "transcript_mode,with_intro", [("words", False), ("absent", False), ("silent", False), ("words", True)]
)
def test_clean_main_and_subtitled_counterpart_share_covering_segments(
    temp_db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transcript_mode: str, with_intro: bool
) -> None:
    monkeypatch.setattr(settings, "clip_loudnorm", False)
    monkeypatch.setattr(settings, "clip_remove_silence", False)
    source = tmp_path / "second.ts"
    _make_black_ts(source, 4)
    base = datetime(2026, 10, 8, tzinfo=UTC)
    with get_session() as db:
        if with_intro:
            db.add(
                IntroTemplate(
                    name="intro", is_default=True, intro_text="INTRO", intro_duration_s=1, intro_font_name="Arial"
                )
            )
        session = RecordingSession(room_id=1, started_at=base)
        db.add(session)
        db.flush()
        earlier = RawSegment(
            session_id=session.id,
            seq=0,
            file_path="not-in-this-cut.ts",
            duration_s=60,
            start_ts=base,
            end_ts=base + timedelta(seconds=60),
        )
        current = RawSegment(
            session_id=session.id,
            seq=1,
            file_path=str(source),
            duration_s=4,
            start_ts=base + timedelta(seconds=60),
            end_ts=base + timedelta(seconds=64),
        )
        db.add_all([earlier, current])
        db.flush()
        db.add(Transcript(segment_id=earlier.id, final_text="wrong earlier text", words_json="[]"))
        if transcript_mode != "absent":
            start, end = (1.1, 2.5) if transcript_mode == "words" else (0.0, 0.5)
            db.add(
                Transcript(
                    segment_id=current.id,
                    final_text="correct local text",
                    words_json=json.dumps([{"w": "CORRECT", "start": start, "end": end}]),
                )
            )
        candidate = HighlightCandidate(
            session_id=session.id,
            peak_ts=base + timedelta(seconds=62),
            start_ts=base + timedelta(seconds=61),
            end_ts=base + timedelta(seconds=63),
            dedup_hash="counterpart-local-timeline",
        )
        db.add(candidate)
        db.flush()
        db.add(HighlightEvent(candidate_id=candidate.id, session_id=session.id))
        candidate_id = candidate.id
    assert candidate_id is not None
    clip = produce_clip(candidate_id, ClipOptions(loudnorm=False, subtitle=False, preset="ultrafast"))
    subtitle_file = Path(clip.file_path).with_suffix(".srt")
    if transcript_mode != "words":
        assert not subtitle_file.exists()
        with get_session() as db:
            variants = db.exec(select(ClipVariant)).all()
            assert variants and all(not item.has_subtitles for item in variants)
            assert not any(item.variant_type == ClipVariantType.SUBTITLED for item in variants)
        assert not (Path(clip.file_path).parent / f"clip_{candidate_id}_subtitled.mp4").exists()
        return
    assert "CORRECT" in subtitle_file.read_text(encoding="utf-8")
    assert "wrong" not in subtitle_file.read_text(encoding="utf-8")
    at_s = 1.4 if with_intro else 0.4
    assert _bright_pixels(Path(clip.file_path), at_s) == 0
    assert _bright_pixels(Path(clip.file_path).parent / f"clip_{candidate_id}_subtitled.mp4", at_s) > 20
    with get_session() as db:
        counterpart = db.exec(select(ClipVariant).where(ClipVariant.variant_type == ClipVariantType.SUBTITLED)).one()
        assert counterpart.has_subtitles is True
        assert Path(counterpart.file_path).is_file()
        if with_intro:
            assert counterpart.duration_s == pytest.approx(3, abs=0.15)
    if with_intro:
        assert clip.duration_s == pytest.approx(3, abs=0.15)
        assert _bright_pixels(Path(clip.file_path), 0.4) > 20
        output = tmp_path / "compute.mp4"
        result = render_clip_to_file(
            candidate_id, output, ClipOptions(loudnorm=False, subtitle=True, preset="ultrafast")
        )
        assert result["duration_s"] == pytest.approx(3, abs=0.15)
        assert result["peak_rel"] == pytest.approx(2, abs=0.1)
        assert _bright_pixels(output, 1.4) > 20
