"""滚动统计数值边界、非连续缓冲区及进一步 Rust 改写的回归。"""

from __future__ import annotations

import math
import random
from collections import UserList
from collections.abc import Sequence
from datetime import datetime, timedelta

import numpy as np
import pytest

from app.accelerators import dispatcher as native
from app.accelerators.python_fallback import speedups_round2 as reference


@pytest.mark.parametrize("current", [0.0, 21.0, 1e308, -1.0, float("nan"), float("inf"), -float("inf")])
@pytest.mark.parametrize(
    "history",
    [
        [],
        [0.0],
        [0.0, 10.0],
        [10.0] * 32,
        [float("nan"), float("inf"), -float("inf")],
        [9.0, float("nan"), 10.0, 11.0],
        [1e308, 1e308],
    ],
)
def test_rolling_filters_nonfinite_and_preserves_boundaries(current: float, history: list[float]) -> None:
    before = history.copy()
    assert native.robust_relative_uplift(current, history) == pytest.approx(
        reference.robust_relative_uplift(current, history), abs=1e-15
    )
    assert all(a == b or math.isnan(a) and math.isnan(b) for a, b in zip(history, before, strict=True))


def test_rolling_random_inputs_non_contiguous_views_and_negative_domain() -> None:
    rng = random.Random(189)
    for size in [1, 2, 3, 31, 32, 33, 128, 513]:
        history = [rng.random() * 100.0 for _ in range(size)]
        current = 55.0
        expected = reference.robust_relative_uplift(current, history)
        for values in [history, tuple(history), np.asarray(history), np.asarray(history[::-1])[::-1]]:
            assert native.robust_relative_uplift(current, values) == pytest.approx(expected, abs=1e-15)
    for history in [[-2.0, -1.0], [-0.001]]:
        with pytest.raises((ValueError, ZeroDivisionError)) as expected_error:
            reference.robust_relative_uplift(1.0, history)
        with pytest.raises(type(expected_error.value)):
            native.robust_relative_uplift(1.0, history)


def test_danmaku_buckets_preserve_unsorted_huge_and_boundary_values() -> None:
    rng = random.Random(189)
    for bucket in [0.1, 1.0, 10.0, -10.0, 1e-20]:
        for size in [0, 9, 10, 33, 1000]:
            timestamps = [rng.choice([-1e100, 1e100, 0.0, 9.9999999, 10.0, 10.0000001, 50.0]) for _ in range(size)]
            before = timestamps.copy()
            assert native.danmaku_baseline_rate(timestamps, bucket) == reference.danmaku_baseline_rate(
                timestamps, bucket
            )
            assert timestamps == before
    for value in [float("nan"), float("inf"), -float("inf")]:
        with pytest.raises((ValueError, OverflowError)) as expected_error:
            reference.danmaku_baseline_rate([0.0] * 10 + [value])
        with pytest.raises(type(expected_error.value)):
            native.danmaku_baseline_rate([0.0] * 10 + [value])
    with pytest.raises(ZeroDivisionError):
        native.danmaku_baseline_rate([0.0] * 10, 0.0)


def test_rust_matrix_weighted_cosine_random_rounding_and_duplicate_keywords() -> None:
    rng = random.Random(189)
    for _ in range(40):
        items = [
            {
                "asr_text": "".join(rng.choices("aabbc中😀 ", k=rng.randrange(120))),
                "keywords": rng.choices(["abc", "中", "", "😀"], k=rng.randrange(8)),
                "start_ts": datetime(2026, 1, 1) + timedelta(microseconds=rng.randrange(3600000001)),
            }
            for _ in range(18)
        ]
        assert native.cluster_similarity_matrix(items) == reference.cluster_similarity_matrix(items)


def test_rust_representatives_keep_first_occurrence_ties() -> None:
    rng = random.Random(187)
    for size in [1, 2, 3, 4, 32, 100, 5000]:
        texts = [f"弹幕{index}" for index in range(size)]
        rng.shuffle(texts)
        texts += texts[:3] * 2
        assert native.danmaku_text_features(texts, ["幕2"]) == reference.danmaku_text_features(texts, ["幕2"])


@pytest.mark.parametrize("texts", [["a", "b"], ("a", "b"), "ab", UserList(["a", "b"])])
def test_danmaku_sequence_protocol_keeps_character_and_container_semantics(texts: Sequence[str]) -> None:
    assert native.danmaku_text_features(texts, "a") == reference.danmaku_text_features(texts, "a")
