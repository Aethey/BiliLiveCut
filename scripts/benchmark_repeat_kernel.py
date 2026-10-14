"""实测重复检测内核；可显式加载旧 Cython 二进制作同机基线。"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import platform
import random
import statistics
import timeit
from collections.abc import Callable
from pathlib import Path

from app.accelerators import _repeat_speedups
from app.accelerators.python_fallback.speedups_round2 import consecutive_repeat_details


def measure(function: Callable[[str], tuple[int, int, int]], text: str) -> float:
    """预热后以五组中位数计时，输入构建不计入调用耗时。"""
    function(text)
    number = 2000 if len(text) < 257 else 16 if len(text) < 10000 else 2
    timer = timeit.Timer(lambda: function(text))
    return statistics.median(timer.repeat(repeat=5, number=number)) / number


def main() -> None:
    """记录原始耗时与倍率，不用固定倍率断言替代功能正确性。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, help="由 PR 原始实现编译的 _cython_speedups 扩展")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    baseline = consecutive_repeat_details
    baseline_name = "Python reference"
    baseline_sha256 = None
    if args.baseline is not None:
        spec = importlib.util.spec_from_file_location("_cython_speedups", args.baseline.resolve())
        if spec is None or spec.loader is None:
            raise RuntimeError("Cannot load the explicit baseline extension")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        baseline = module.consecutive_repeat_details
        baseline_name = "PR original Cython"
        baseline_sha256 = hashlib.sha256(args.baseline.read_bytes()).hexdigest()
    rng = random.Random(8003)
    cases = {
        "empty": "",
        "short-normal": "这里是一次普通的直播讲解，没有连续三次相同短语。",
        "short-loop": "中😀\0" * 16,
        "boundary-256": "".join(rng.choices("abcdef", k=256)),
        "boundary-257": "".join(rng.choices("abcdef", k=257)),
        "long-whole-period": "".join(chr(0x1000 + i) for i in range(128)) * 1000,
        "long-local-period": "prefix" + "".join(chr(0x2000 + i) for i in range(64)) * 2000 + "suffix",
        "long-uniform": "中" * 128000,
    }
    for size in (16000, 32000, 64000, 128000):
        cases[f"noise-{size}"] = "".join(rng.choices("a中😀ß", k=size))
    results = []
    for name, text in cases.items():
        expected = baseline(text)
        assert _repeat_speedups.consecutive_repeat_details(text) == expected, name
        old = measure(baseline, text)
        current = measure(_repeat_speedups.consecutive_repeat_details, text)
        row = {
            "case": name,
            "codepoints": len(text),
            "baseline_seconds": old,
            "kernel_seconds": current,
            "speedup": old / current,
        }
        results.append(row)
        print(f"{name}: {old / current:.2f}x; native {current * 1000:.4f} ms", flush=True)
    report = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "baseline": baseline_name,
        "baseline_sha256": baseline_sha256,
        "kernel_sha256": hashlib.sha256(Path(_repeat_speedups.__file__).read_bytes()).hexdigest(),
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
