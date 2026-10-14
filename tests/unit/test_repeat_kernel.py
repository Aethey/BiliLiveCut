"""独立 C++ 内核的可观察行为：穷举、Unicode、周期边界和并发调用。"""

from __future__ import annotations

import hashlib
import itertools
import json
import random
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.accelerators import _repeat_speedups as kernel
from app.accelerators.python_fallback.speedups_round2 import consecutive_repeat_details as reference


def brute(text: str) -> tuple[int, int, int]:
    """小输入独立穷举：覆盖优先，周期次之，再比较起点。"""
    best = (0, 0, 0)
    for period in range(1, len(text) // 3 + 1):
        for start in range(len(text) - 3 * period + 1):
            unit = text[start : start + period]
            end = start + period
            while text[end : end + period] == unit:
                end += period
            count = (end - start) // period
            if count >= 3 and count * period > best[1] * best[2]:
                best = (start, period, count)
    return best


@pytest.mark.parametrize("length", range(10))
def test_kernel_matches_exhaustive_unicode(length: int) -> None:
    for values in itertools.product("a中😀", repeat=length):
        text = "".join(values)
        assert kernel.consecutive_repeat_details(text) == brute(text)


@pytest.mark.parametrize("length", [255, 256, 257, 258, 319, 511, 512, 513])
def test_kernel_base_case_and_rmq_boundaries(length: int) -> None:
    rng = random.Random(length)
    alphabet = "a中😀\ud800ßİ\0 \U0010ffff"
    for _ in range(25):
        unit = "".join(rng.choices(alphabet, k=rng.randrange(1, 90)))
        text = "".join(rng.choices(alphabet, k=length))
        offset = rng.randrange(length // 3)
        text = (text[:offset] + unit * rng.randrange(3, 8) + text[offset:])[:length]
        assert kernel.consecutive_repeat_details(text) == brute(text)


def test_kernel_long_patterns_and_partial_runs() -> None:
    rng = random.Random(803)
    for size in (300, 1024, 4096, 10000):
        noise = "".join(rng.choices("a中😀\udfffß\0", k=size))
        unit = "".join(chr(0x20000 + i) for i in range(size // 13))
        for text in (noise, unit * 4, "X" + unit * 4 + unit[:7] + noise, noise + unit * 5, ("ab" * size) + "c"):
            assert kernel.consecutive_repeat_details(text) == reference(text)


def test_kernel_tie_breaking_and_all_codepoints() -> None:
    cases = [
        ("abcabcabcXYZdefdefdef", (0, 3, 3)),
        ("abc" * 4 + "X" + "q" * 12, (13, 1, 12)),
        ("x" * 6 + "Y" + "x" * 6, (0, 1, 6)),
        ("\ud800\0😀" * 100, (0, 3, 100)),
        ("abc" * 2, (0, 0, 0)),
        ("", (0, 0, 0)),
    ]
    for text, expected in cases:
        assert kernel.consecutive_repeat_details(text) == expected
    unique = "".join(map(chr, range(0x110000)))
    assert kernel.consecutive_repeat_details(unique) == (0, 0, 0)


def test_kernel_rejects_non_string_and_is_reentrant() -> None:
    for value in (None, b"aaa", 123, ["a"]):
        with pytest.raises(TypeError):
            kernel.consecutive_repeat_details(value)
    values = ["X" + ("中😀\0" + str(i)) * 200 + "Z" for i in range(16)]
    expected = [reference(value) for value in values]
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(kernel.consecutive_repeat_details, values)) == expected


@pytest.mark.parametrize("length", range(9))
def test_kernel_lyndon_path_matches_exhaustive_oracle(length: int) -> None:
    padding = "".join(chr(0x1000 + index) for index in range(257))
    for values in itertools.product("a中😀", repeat=length):
        text = "".join(values)
        start, period, count = brute(text)
        expected = (start + len(padding), period, count) if count else (0, 0, 0)
        assert kernel.consecutive_repeat_details(padding + text) == expected


def test_vendored_suffix_array_sources_match_reviewed_hashes() -> None:
    root = Path(__file__).resolve().parents[2] / "tools/native/repeat/vendor/libsais"
    lock = json.loads((root / "UPSTREAM.json").read_text(encoding="utf-8"))
    assert lock["commit"] == "ce90878d784b5ff7d019300535675e4a2e22aae0"
    assert lock["openmp"] is False and lock["license"] == "Apache-2.0"
    assert set(lock["files"]) == {"src/libsais.c", "include/libsais.h", "LICENSE"}
    for name, entry in lock["files"].items():
        data = (root / name).read_bytes()
        assert len(data) == entry["vendored_bytes"]
        assert hashlib.sha256(data).hexdigest() == entry["vendored_sha256"]
