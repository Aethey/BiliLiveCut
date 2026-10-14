"""CPU ASR 统计与旧判定的精确回归；包括 Unicode、平局和风险阈值。"""

from __future__ import annotations

import itertools
import random
from typing import TYPE_CHECKING

import pytest

from app.accelerators import dispatcher as native
from app.accelerators.python_fallback import speedups_round2 as reference
from app.analysis.transcription.backends import _compute_review_risk_score
from app.analysis.transcription.models import ASRSegmentResult
from app.analysis.transcription.quality import _normalize_with_indexes, repair_local_decode_loop

if TYPE_CHECKING:
    from pytest import MonkeyPatch


def brute_repeat_details(text: str) -> tuple[int, int, int]:
    """仅供小输入对照的穷举算法，周期长度不设 32 字符上限。"""
    if len(text) > 512:
        raise ValueError("穷举参考仅用于小输入")
    best = (0, 0, 0)
    best_coverage = 0
    for unit_length in range(1, len(text) // 3 + 1):
        for start in range(len(text) - unit_length * 3 + 1):
            unit = text[start : start + unit_length]
            cursor = start + unit_length
            repeats = 1
            while text[cursor : cursor + unit_length] == unit:
                repeats += 1
                cursor += unit_length
            coverage = repeats * unit_length
            if repeats >= 3 and coverage > best_coverage:
                best = (start, unit_length, repeats)
                best_coverage = coverage
    return best


def previous_review_risk_score(
    segment: ASRSegmentResult,
    hotwords: list[str] | None = None,
) -> tuple[float, list[str]]:
    """计算句子的复核风险评分 (0-1), 越高越需要复核。

    综合信号:
    1. 置信度缺失或极低
    2. 空文本或极短
    3. 文本与音频时长比异常
    4. 重复字符/短语比例过高
    5. 乱码/非预期符号
    6. 热词冲突检测

    :param segment: 句子级 ASR 结果。
    :param hotwords: 房间热词列表。
    :returns: ``(risk_score, reasons)``。
    """
    risk = 0.0
    reasons: list[str] = []

    # 1) 置信度
    if not segment.confidence_available:
        risk += 0.15
        reasons.append("confidence_unavailable")
    elif segment.normalized_confidence is not None:
        conf_risk = max(0.0, 1.0 - segment.normalized_confidence)
        if conf_risk > 0.3:
            risk += conf_risk * 0.5
            reasons.append(f"low_confidence({segment.normalized_confidence:.2f})")

    # 2) 空文本或极短
    text = segment.text.strip()
    if not text:
        risk += 0.5
        reasons.append("empty_text")
    elif len(text) <= 2:
        risk += 0.3
        reasons.append("very_short_text")

    # 3) 文本与音频时长比异常
    duration = segment.end - segment.start
    if duration > 0.5 and len(text) > 0:
        chars_per_sec = len(text) / duration
        if chars_per_sec < 0.5:  # 很长的时间只有很少的字
            risk += 0.3
            reasons.append(f"low_chars_per_sec({chars_per_sec:.1f})")
        elif chars_per_sec > 20:  # 很短时间有大量字
            risk += 0.2
            reasons.append(f"high_chars_per_sec({chars_per_sec:.1f})")

    # 4) 重复字符/短语
    if len(text) >= 4:
        unique_ratio = len(set(text)) / len(text)
        if unique_ratio < 0.3:
            risk += 0.3
            reasons.append("high_repetition")

    # 5) 乱码/非预期符号
    non_cjk = sum(1 for c in text if ord(c) < 32 or (ord(c) > 126 and ord(c) < 0x4E00))
    if non_cjk > len(text) * 0.3:
        risk += 0.2
        reasons.append("possible_garbled")

    # 6) 热词冲突: 文本中包含热词前缀但整体不匹配
    if hotwords:
        for hw in hotwords:
            if len(hw) >= 1:
                # 检查热词的任意前缀或单字是否出现在文本中
                for length in range(1, len(hw) + 1):
                    sub = hw[:length]
                    if len(sub) >= 1 and sub in text and hw not in text:
                        risk += 0.15
                        reasons.append(f"hotword_conflict({hw}->{sub})")
                        break
                else:
                    continue
                break

    return min(risk, 1.0), reasons


@pytest.mark.parametrize("length", range(9))
def test_repeat_exhaustive_short_text(length: int) -> None:
    for characters in itertools.product("a中😀", repeat=length):
        text = "".join(characters)
        expected = brute_repeat_details(text)
        assert reference.consecutive_repeat_details(text) == expected
        assert native.consecutive_repeat_details(text) == expected


def test_repeat_random_periods_ties_and_unicode() -> None:
    rng = random.Random(1829)
    alphabet = "a中😀\ud800ßİ\0 "
    for _ in range(350):
        unit = "".join(rng.choices(alphabet, k=rng.randrange(1, 36)))
        text = "".join(rng.choices(alphabet, k=rng.randrange(40))) + unit * rng.randrange(1, 10)
        expected = brute_repeat_details(text)
        assert reference.consecutive_repeat_details(text) == expected
        assert native.consecutive_repeat_details(text) == expected
    assert native.consecutive_repeat_details("abcabcabcXYZdefdefdef") == (0, 3, 3)


def test_casefold_indexes_cover_expanded_characters() -> None:
    normalized, indexes = _normalize_with_indexes("A!ß İ𐐀")
    assert normalized == "assi\u0307𐐨"
    assert indexes == [0, 2, 2, 4, 4, 5]
    text = "".join(chr(index) for index in range(0x10000))
    normalized, indexes = _normalize_with_indexes(text)
    assert len(normalized) == len(indexes)
    from collections import Counter

    for raw_index, count in Counter(indexes).items():
        assert count == len(text[raw_index].casefold())


def test_local_repair_after_casefold_expansion_keeps_normal_suffix() -> None:
    prefix = "".join(f"主播ß正在解释第{index}段不同的正常内容。" for index in range(20))
    suffix = "随后还有完全正常且需要保留的内容。"
    result = repair_local_decode_loop(prefix + "ßabc" * 4 + suffix)
    assert result.changed
    assert result.text == prefix + "ßabc" * 2 + suffix
    assert result.removed_characters == 8


@pytest.mark.parametrize("text", ["", "中", "哈哈哈哈", "\0\x1f Aé\u4dff中😀\ud800", "原神与原始", "原神完整"])
@pytest.mark.parametrize("hotwords", [None, [], [""], ["原神", "原始人"], ["不存在" * 100, "é中"], ["中", "中中"]])
def test_review_statistics_exact(text: str, hotwords: list[str] | None) -> None:
    assert native.review_text_statistics(text, hotwords) == reference.review_text_statistics(text, hotwords)


def test_review_risk_matches_original_decisions_and_reason_order() -> None:
    rng = random.Random(73)
    for _ in range(800):
        text = "".join(rng.choices("a中原神\0é😀\ud800 ", k=rng.randrange(150)))
        segment = ASRSegmentResult(
            start=0.0,
            end=rng.choice([0.0, 0.5, 0.50001, 1.0, 60.0]),
            text=text,
            normalized_confidence=rng.choice([None, 0.0, 0.3, 0.699999, 0.7, 1.0]),
            confidence_available=rng.choice([False, True]),
        )
        hotwords = ["", "原神", "abc", "é中"]
        actual = _compute_review_risk_score(segment, hotwords)
        assert actual == previous_review_risk_score(segment, hotwords)


def test_asr_statistics_python_only_backend(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(native, "_cython_backend", None)
    assert native.consecutive_repeat_details("abc" * 4) == (0, 3, 4)
    unit = "".join(chr(0x4E00 + index) for index in range(64))
    assert native.consecutive_repeat_details(unit * 3) == (0, 64, 3)
    assert native.review_text_statistics("原原原原", ["原神"]) == (1, 0, "原神")


@pytest.mark.parametrize("function", [reference.consecutive_repeat_details, native.consecutive_repeat_details])
@pytest.mark.parametrize("period", [32, 33, 64, 257, 4096])
def test_unrestricted_period_with_context_and_partial_suffix(function, period: int) -> None:
    unit = "".join(chr(0x4E00 + index) for index in range(period))
    assert function("前😀" + unit * 4 + unit[:5] + "🛑后") == (2, period, 4)


@pytest.mark.parametrize("function", [reference.consecutive_repeat_details, native.consecutive_repeat_details])
@pytest.mark.parametrize(
    "text,expected",
    [
        ("", (0, 0, 0)),
        ("aa", (0, 0, 0)),
        ("aaa", (0, 1, 3)),
        ("abab", (0, 0, 0)),
        ("abc" * 3 + "X" + "a" * 9, (10, 1, 9)),
        ("abc" * 3 + "X" + "def" * 3, (0, 3, 3)),
        ("ababababa", (0, 2, 4)),
        ("a\0\ud800😀" * 4, (0, 4, 4)),
    ],
)
def test_repeat_ties_minimum_count_and_unicode(function, text: str, expected: tuple[int, int, int]) -> None:
    assert function(text) == expected


@pytest.mark.timeout(60)
@pytest.mark.parametrize("function", [reference.consecutive_repeat_details, native.consecutive_repeat_details])
def test_long_inputs_do_not_rescan_repeated_suffixes(function) -> None:
    assert function("a" * 100000) == (0, 1, 100000)
    assert function("前😀" + "a" * 100000 + "🛑后") == (2, 1, 100000)
    assert function("".join(chr(index) for index in range(20000))) == (0, 0, 0)


def test_repetition_prefix_matching_work_grows_nlogn(monkeypatch: MonkeyPatch) -> None:
    original = reference._repeat_z
    work = 0

    def counted(symbols: list[str]) -> list[int]:
        nonlocal work
        work += len(symbols)
        return original(symbols)

    monkeypatch.setattr(reference, "_repeat_z", counted)
    for size in [1024, 2048, 4096]:
        work = 0
        assert reference.consecutive_repeat_details("".join(chr(index) for index in range(size))) == (0, 0, 0)
        assert work <= 6 * size * size.bit_length()


@pytest.mark.parametrize("force_python", [False, True])
def test_long_period_quality_and_local_repair(force_python: bool, monkeypatch: MonkeyPatch) -> None:
    from app.analysis.transcription.quality import assess_transcript_quality

    if force_python:
        monkeypatch.setattr(native, "_cython_backend", None)
    unit = "".join(chr(0x4E00 + index) for index in range(80))
    prefix = "".join(chr(0x6000 + index) for index in range(700))
    suffix = "".join(chr(0x7000 + index) for index in range(700))
    assert assess_transcript_quality(unit * 3).reason == "degenerate_repetition"
    result = repair_local_decode_loop(prefix + unit * 4 + suffix)
    assert result.changed
    assert result.text == prefix + unit * 2 + suffix
    assert result.removed_characters == 160
    assert assess_transcript_quality(result.text).usable
