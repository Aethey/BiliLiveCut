#!/usr/bin/env python3
"""Local CI gate — replicate CI checks from the command line.

按顺序复现 CI lint + audit + test + portable 全部检查。
任一步骤失败都会关闭门禁并最终返回非零退出码 (fail-closed)。

用法:
    python scripts/ci_gate.py
    python scripts/ci_gate.py --skip-portable
    python scripts/ci_gate.py --skip-coverage
    python scripts/ci_gate.py --skip-audit
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"


def _run(cmd: list[str], cwd: str | None = None, desc: str = "", env: dict[str, str] | None = None) -> bool:
    """Run a command and report success/failure.

    :param cmd: Command and args list.
    :param cwd: Working directory (default: REPO_ROOT).
    :param desc: Human-readable description.
    :param env: Optional child-process environment.
    :returns: True if command succeeded (exit 0).
    """
    print(f"\n{YELLOW}[{desc}]{RESET}")
    print(f"  $ {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=cwd or str(REPO_ROOT), env=env)
    if result.returncode == 0:
        print(f"{GREEN}  PASS{RESET}")
        return True
    else:
        print(f"{RED}  FAIL (exit={result.returncode}){RESET}")
        return False


def _pytest(path: str, extra_args: list[str] | None = None, desc: str = "") -> bool:
    """Run pytest on a path.

    :param path: Test path.
    :param extra_args: Additional pytest arguments.
    :param desc: Description string.
    :returns: True if all tests passed.
    """
    suite_name = "portable" if path.startswith("packaging/portable") else "main"
    temp_root = REPO_ROOT / "packaging" / "portable" / "build" / "ci-gate"
    temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"{suite_name}-", dir=temp_root, ignore_cleanup_errors=True) as run_dir:
        run_root = Path(run_dir)
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            path,
            "-v",
            "--timeout=120",
            "--fail-on-skip",
            f"--basetemp={run_root / 'pytest'}",
            "-o",
            f"cache_dir={run_root / 'cache'}",
        ]
        if extra_args:
            cmd.extend(extra_args)
        env = os.environ.copy()
        env.setdefault("ASR_NO_MODEL_DOWNLOAD", "1")
        return _run(cmd, desc=desc or f"pytest {path}", env=env)


def main() -> int:
    """Entry point for local CI gate.

    :returns: 0 if all checks pass, 1 if any fail.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-portable", action="store_true", help="Run a partial gate without Windows Payload/tests")
    parser.add_argument("--skip-coverage", action="store_true", help="Run a partial gate without coverage measurement")
    parser.add_argument("--skip-audit", action="store_true", help="Run a partial gate without dependency auditing")
    args = parser.parse_args()

    print("=" * 60)
    print("  BiliLiveCut Local CI Gate")
    print("=" * 60)
    print(f"  Python: {sys.version}")
    print(f"  Root:   {REPO_ROOT}")
    print("=" * 60)

    all_ok = True

    # 同一配置覆盖本地提交和 CI 的全部轻量检查。
    all_ok &= _run(
        [sys.executable, "-m", "pre_commit", "run", "--all-files", "--show-diff-on-failure"],
        desc="pre-commit quality checks",
    )

    # 联网依赖审计。
    if not args.skip_audit:
        all_ok &= _run(
            [sys.executable, "scripts/audit_portable_runtime_locks.py"],
            desc="Portable runtime lock audit",
        )

    # 主测试与可选的覆盖率测量。
    cov_args = ["--cov=app", "--cov-report=term-missing", "--cov-fail-under=50"]
    if args.skip_coverage:
        cov_args = []
    all_ok &= _pytest(
        "tests/",
        extra_args=cov_args,
        desc="pytest tests/" if args.skip_coverage else "pytest tests/ (coverage >= 50%)",
    )

    # 先构建再验收 Portable。
    if not args.skip_portable:
        payload_built = _run(
            [sys.executable, "packaging/portable/build_payload.py"],
            desc="Build Windows Payload before Portable tests",
        )
        all_ok &= payload_built
        if payload_built:
            all_ok &= _pytest("packaging/portable/tests/", desc="pytest packaging/portable/tests/")
        else:
            print(f"{RED}  Payload build failed; refusing to test stale artifacts{RESET}")

    # 部分检查不能声明完整 CI 通过。
    print(f"\n{'=' * 60}")
    partial = args.skip_portable or args.skip_coverage or args.skip_audit
    if all_ok and partial:
        print(f"{YELLOW}  SELECTED CHECKS PASSED — full CI gate not evaluated{RESET}")
    elif all_ok:
        print(f"{GREEN}  ALL CHECKS PASSED — CI gate open{RESET}")
    else:
        print(f"{RED}  SOME CHECKS FAILED — CI gate closed{RESET}")
    print(f"{'=' * 60}")

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
