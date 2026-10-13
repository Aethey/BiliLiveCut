"""CPU 聚类/新颖度的数值边界、Unicode 等价性与真实数据库回归。"""

from __future__ import annotations

import copy
import math
import random
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlmodel import select

from app.accelerators import dispatcher
from app.accelerators.python_fallback import speedups_round2 as reference

if TYPE_CHECKING:
    from pytest import MonkeyPatch


@pytest.mark.parametrize("function", [reference.cluster_groups, dispatcher.cluster_groups])
def test_cluster_transitivity_root_order_and_all_internal_edges(
    function: Callable[[list[list[float]], float], list[tuple[int, list[int], float]]],
) -> None:
    matrix = [[1.0 if i == j else 0.0 for j in range(6)] for i in range(6)]
    for i, j in [(0, 3), (1, 2), (2, 3), (4, 5)]:
        matrix[i][j] = matrix[j][i] = 0.82
    before = copy.deepcopy(matrix)
    groups = function(matrix, 0.82)
    assert groups == [(1, [0, 1, 2, 3], (0.82 + 0.82 + 0.82) / 6), (4, [4, 5], 0.82)]
    assert matrix == before
    assert function(matrix, math.nextafter(0.82, math.inf)) == []


@pytest.mark.parametrize("function", [reference.cluster_groups, dispatcher.cluster_groups])
@pytest.mark.parametrize(
    "matrix,threshold",
    [([[0.0, 1.0]], 0.82), ([[math.nan]], 0.82), ([[math.inf]], 0.82), ([], math.nan), ([], math.inf)],
)
def test_cluster_rejects_invalid_shape_and_nonfinite_values(
    function: Callable[[list[list[float]], float], list[tuple[int, list[int], float]]],
    matrix: list[list[float]],
    threshold: float,
) -> None:
    with pytest.raises(ValueError, match="square and finite"):
        function(matrix, threshold)


def test_cluster_random_matrices_match_original_accumulation_order() -> None:
    rng = random.Random(18193)
    for size in [0, 1, 2, 3, 8, 17, 32, 64]:
        for threshold in [0.0, 0.6, 0.82, 1.0]:
            matrix = [[round(rng.random(), 4) for _ in range(size)] for _ in range(size)]
            assert dispatcher.cluster_groups(matrix, threshold) == reference.cluster_groups(matrix, threshold)


@pytest.mark.parametrize("include_entities", [False, True])
@pytest.mark.parametrize(
    "current,baseline",
    [
        ("", ""),
        ("", "中文abc"),
        ("中文", ""),
        ("中a文B", "a b"),
        ("中文文本中文", "文本直播"),
        ("_ __ ________ _________", "__"),
        ("中文测试超过八个字的实体", "中文测试"),
        ("İKABCßΣς𠀀😀\ud800", "i k abc"),
        ("甲乙\x00ABC_123中文", "ABC_123甲乙"),
        ("１２３", "123"),
    ],
)
def test_text_novelty_preserves_unicode_rules(current: str, baseline: str, include_entities: bool) -> None:
    assert dispatcher.text_novelty(current, baseline, include_entities=include_entities) == reference.text_novelty(
        current, baseline, include_entities=include_entities
    )


def test_text_novelty_known_scores_and_missing_entity_statistic() -> None:
    assert dispatcher.text_novelty("AB 新鲜", "ab 日常") == (0.5, 1.0 - 1.0 / 3.0, 0.5)
    assert dispatcher.text_novelty("AB 新鲜", "ab 日常", include_entities=False) == (0.5, 1.0 - 1.0 / 3.0, None)
    # lower 先于词项扫描，但实体的正则扫描先于 lower。
    assert dispatcher.text_novelty("İK", "i k") == (0.0, 0.0, 0.0)


def test_text_novelty_seeded_mixed_unicode_and_full_bmp() -> None:
    rng = random.Random(18194)
    alphabet = list("中中文本直播ABab01_!? \n\tİKΣß😀") + ["\ud800", "\udfff", "\u4dff", "\u9fff", "\ua000"]
    for size in [1, 2, 8, 32, 128, 1024, 4096]:
        current = "".join(rng.choices(alphabet, k=size))
        baseline = "".join(rng.choices(alphabet, k=size // 2))
        assert dispatcher.text_novelty(current, baseline) == reference.text_novelty(current, baseline)
    text = "".join(chr(value) for value in range(65536))
    assert dispatcher.text_novelty(text, text[10000:]) == reference.text_novelty(text, text[10000:])


def test_real_topic_clustering_keeps_database_links_idempotent(temp_db: None) -> None:
    from app.analysis.topic_cluster import cluster_candidates
    from app.db.entities import HighlightCandidate, HighlightEvent, HighlightTopic, Topic
    from app.db.session import get_session

    start = datetime(2026, 10, 13)
    with get_session() as db:
        for index, text in enumerate(["相同中文直播", "相同中文直播", "完全不同的主题"]):
            candidate = HighlightCandidate(
                session_id=73,
                start_ts=start,
                peak_ts=start,
                end_ts=start + timedelta(seconds=10),
                features_json='{"keyword_hits":["共同"]}' if index < 2 else '{"keyword_hits":["其他"]}',
                dedup_hash=f"cluster-cpu-{index}",
            )
            db.add(candidate)
            db.flush()
            assert candidate.id is not None
            db.add(HighlightEvent(candidate_id=candidate.id, session_id=73, asr_text=text))
    first = cluster_candidates(73)
    assert len(first) == 1
    assert first[0]["title"] == "共同"
    assert first[0]["event_count"] == 2
    assert first[0]["confidence"] == 1.0
    assert cluster_candidates(73) == first
    with get_session() as db:
        assert len(db.exec(select(Topic)).all()) == 1
        assert len(db.exec(select(HighlightTopic)).all()) == 2


def test_source_mode_without_native_extensions_keeps_same_results(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(dispatcher, "_cython_backend", None)
    monkeypatch.setattr(dispatcher, "_rust_backend", None)
    assert dispatcher.text_novelty("AB 新鲜", "ab 日常") == (0.5, 1.0 - 1.0 / 3.0, 0.5)
    assert dispatcher.cluster_groups([[1.0, 0.9], [0.9, 1.0]], 0.82) == [(0, [0, 1], 0.9)]
