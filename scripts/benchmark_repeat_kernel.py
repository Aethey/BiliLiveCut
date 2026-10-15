"""以同机 main 原生模块为等价语义基线，单独记录新增任意周期能力。"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import platform
import random
import re
import statistics
import timeit
from collections.abc import Callable
from pathlib import Path

from app.accelerators import _repeat_speedups


def measure(function: Callable[[str], tuple[int, int, int]], text: str) -> float:
    """预热校准到每组至少约 20ms 后取七组中位数，输入生成不计时。"""
    function(text)
    number = 2000 if len(text) < 257 else 16 if len(text) < 10000 else 3
    timer = timeit.Timer(lambda: function(text))
    elapsed = timer.timeit(number=number)
    number = max(number, math.ceil(number * 0.02 / max(elapsed, 1e-9)))
    return statistics.median(timer.repeat(repeat=7, number=number)) / number


def main() -> None:
    """核对每组返回值一致，再记录可追溯基线和实际性能结果。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True, help="由指定 main 提交编译的 _cython_speedups 扩展")
    parser.add_argument("--baseline-commit", required=True, help="基线的完整 main 提交 SHA")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--check-no-regression", action="store_true", help="任一等价样本中位耗时增加时返回非零")
    args = parser.parse_args()
    if re.fullmatch(r"[0-9a-f]{40}", args.baseline_commit) is None:
        parser.error("--baseline-commit must be a full lowercase commit SHA")
    spec = importlib.util.spec_from_file_location("_cython_speedups", args.baseline.resolve())
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load the explicit main baseline extension")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    baseline = module.consecutive_repeat_details
    rng = random.Random(8003)
    cases = {
        "empty": "",
        "short-normal": "这里是一次普通的直播讲解，没有连续三次相同短语。",
        "short-loop": "中😀\0" * 16,
    }
    for size in (256, 257, 1000, 16000, 32000, 64000, 128000):
        cases[f"noise-{size}"] = "".join(rng.choices("a中😀ß", k=size))
    boundary_rng = random.Random(80512)
    for size in (511, 512, 513):
        cases[f"noise-{size}"] = "".join(boundary_rng.choices("a中😀ß", k=size))
    cases.update(
        {
            "long-whole-period-16": "abcdefghijklmnop" * 8000,
            "long-local-period-16": "prefix" + "abcdefghijklmnop" * 8000 + "suffix",
            "long-uniform": "中" * 128000,
        }
    )
    results = []
    for name, text in cases.items():
        expected = baseline(text)
        assert _repeat_speedups.consecutive_repeat_details(text) == expected, name
        old = measure(baseline, text)
        current = measure(_repeat_speedups.consecutive_repeat_details, text)
        results.append(
            {
                "case": name,
                "codepoints": len(text),
                "result": expected,
                "baseline_seconds": old,
                "kernel_seconds": current,
                "speedup": old / current,
            }
        )
        print(f"{name}: {old / current:.2f}x; native {current * 1000:.4f} ms", flush=True)
    extended = []
    for period in (33, 64, 128, 257):
        unit = "".join(chr(0x2000 + index) for index in range(period))
        text = "prefix" + unit * 1000 + "suffix"
        expected = (6, period, 1000)
        assert _repeat_speedups.consecutive_repeat_details(text) == expected
        extended.append(
            {
                "period": period,
                "codepoints": len(text),
                "result": expected,
                "kernel_seconds": measure(_repeat_speedups.consecutive_repeat_details, text),
            }
        )
    report = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "baseline": "main Cython (period <=32)",
        "baseline_commit": args.baseline_commit,
        "baseline_sha256": hashlib.sha256(args.baseline.read_bytes()).hexdigest(),
        "kernel_sha256": hashlib.sha256(Path(_repeat_speedups.__file__).read_bytes()).hexdigest(),
        "kernel_source_sha256": hashlib.sha256(
            (Path(__file__).resolve().parents[1] / "tools/native/repeat/repeat.cpp").read_bytes()
        ).hexdigest(),
        "results": results,
        "extended_semantics": extended,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.check_no_regression and any(row["speedup"] < 1 for row in results):
        raise SystemExit("FAIL: measured regression against the specified main baseline")


if __name__ == "__main__":
    main()
