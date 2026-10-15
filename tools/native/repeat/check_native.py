"""在 Linux GCC 上编译独立 CPU 内核并执行 AddressSanitizer/UBSan 对照验收。"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


def main() -> None:
    """使用临时目录，启用内存泄漏/未定义行为检查，任一失败立即返回非零。"""
    root = Path(__file__).resolve().parent
    flags = ["-O1", "-g", "-fsanitize=address,undefined", "-fno-omit-frame-pointer"]
    with tempfile.TemporaryDirectory(prefix="blc-repeat-sanitize-") as temporary:
        work = Path(temporary)
        vendor = work / "libsais.o"
        subprocess.run(
            [
                "gcc",
                "-std=c99",
                *flags,
                "-I",
                str(root / "vendor/libsais/include"),
                "-c",
                str(root / "vendor/libsais/src/libsais.c"),
                "-o",
                str(vendor),
            ],
            check=True,
        )
        executable = work / "repeat-check"
        subprocess.run(
            [
                "g++",
                "-std=c++17",
                *flags,
                "-Wall",
                "-Wextra",
                "-Werror",
                "-I",
                str(root / "vendor/libsais/include"),
                str(root / "repeat.cpp"),
                str(root / "repeat_sanitizer.cpp"),
                str(vendor),
                "-o",
                str(executable),
            ],
            check=True,
        )
        subprocess.run(
            [str(executable)],
            check=True,
            env={
                **os.environ,
                "ASAN_OPTIONS": "detect_leaks=1:halt_on_error=1",
                "UBSAN_OPTIONS": "halt_on_error=1:print_stacktrace=1",
            },
        )


if __name__ == "__main__":
    main()
