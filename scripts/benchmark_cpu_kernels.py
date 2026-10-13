"""串行比较完整 CPU 内核调用；合成基准不代表 ASR/CUDA 端到端吞吐。"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import timeit
from collections.abc import Callable
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.accelerators import dispatcher as native  # noqa: E402
from app.accelerators.python_fallback import speedups_round2 as reference  # noqa: E402


def duration(function: Callable[[], object], count: int) -> float:
    """预热后报告七轮中位数微秒，包括 Python 参数转换与返回值处理。"""
    function()
    return statistics.median(timeit.repeat(function, number=count, repeat=7)) / count * 1e6


def previous_docker_matrix(items: list[dict[str, object]]) -> list[list[float]]:
    """重现原 Docker 分派调用边界，不能省略其 Python 包装层后与新入口比较。"""
    if native._cython_backend is not None:
        return native._cython_backend.cluster_similarity_matrix(items)
    return reference.cluster_similarity_matrix(items)


def benchmark() -> list[dict[str, object]]:
    """要求真实原生扩展，校验结果相等，再逐项测量大小输入。"""
    if native.get_cython_backend() != "Cython" or native.get_rust_backend() != "Rust+rayon":
        raise RuntimeError("基准要求真实 Cython 和 Rust 扩展，不能用参考实现冒充")
    rows: list[dict[str, object]] = []

    def measure(name: str, size: int, before: Callable[[], object], after: Callable[[], object], count: int) -> None:
        if before() != after():
            raise AssertionError(f"{name}/{size} 输出不一致")
        baseline_us = duration(before, count)
        native_us = duration(after, count)
        rows.append(
            {
                "kernel": name,
                "size": size,
                "baseline_us": baseline_us,
                "native_us": native_us,
                "ratio": native_us / baseline_us,
            }
        )

    for size in [0, 1, 8, 64, 1024, 65536]:
        raw = bytes([0, 128]) * size
        measure(
            "pcm",
            size,
            partial(reference.pcm16_statistics, raw),
            partial(native.pcm16_statistics, raw),
            10000 if size < 100 else (500 if size < 2000 else 15),
        )
    for size in [0, 1, 8, 64, 4096]:
        texts = ["高能! 😀" if index % 3 else "日常测试" for index in range(size)]
        tokens = ["高能", "笑死"]
        measure(
            "danmaku",
            size,
            partial(reference.danmaku_text_features, texts, tokens),
            partial(native.danmaku_text_features, texts, tokens),
            2000 if size < 100 else 20,
        )
    for size in [0, 1, 2, 4, 8, 16, 32, 33, 64, 128]:
        items = [
            {
                "asr_text": f"中文直播游戏第{index % 7}个操作！",
                "keywords": ["游戏", str(index % 5)],
                "start_ts": datetime(2026, 1, 1) + timedelta(seconds=index * 10),
            }
            for index in range(size)
        ]
        measure(
            "matrix",
            size,
            partial(previous_docker_matrix, items),
            partial(native.cluster_similarity_matrix, items),
            500 if size < 8 else (50 if size < 64 else 3),
        )
    return rows


def main() -> int:
    """将平台、真实后端与原始测量输出为 JSON，不把局部加速称为整体加速。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = {"platform": platform.platform(), "python": platform.python_version(), "rows": benchmark()}
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
