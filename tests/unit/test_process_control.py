"""可取消子进程执行工具测试。"""

from __future__ import annotations

import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psutil
import pytest

from app.core.process_control import ProcessCancelledError, ProcessShutdownError, run_cancellable


def test_run_cancellable_returns_completed_process() -> None:
    """正常命令返回标准 CompletedProcess。"""
    result = run_cancellable(
        [sys.executable, "-c", "print('ok')"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "ok"


def test_run_cancellable_terminates_process_on_cancel() -> None:
    """取消检查为真后迅速终止子进程。"""
    started = time.monotonic()

    with pytest.raises(ProcessCancelledError):
        run_cancellable(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cancel_check=lambda: time.monotonic() - started > 0.2,
            timeout=10,
            poll_interval_s=0.05,
        )

    assert time.monotonic() - started < 5


def test_run_cancellable_enforces_timeout() -> None:
    """超时会终止进程并抛出 TimeoutExpired。"""
    with pytest.raises(subprocess.TimeoutExpired):
        run_cancellable(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=0.2,
            poll_interval_s=0.05,
        )


def test_shutdown_refuses_new_command_before_launch(tmp_path: Path) -> None:
    from app.pipeline.lifecycle import shutdown_event

    marker = tmp_path / "unexpected.txt"
    shutdown_event.set()
    try:
        with pytest.raises(ProcessShutdownError):
            run_cancellable([sys.executable, "-c", f"from pathlib import Path;Path({str(marker)!r}).touch()"])
        assert not marker.exists()
    finally:
        shutdown_event.clear()


def _descendant_command(pid_file: Path, *, parent_exit: bool = False) -> list[str]:
    script = (
        "import subprocess,sys,time; from pathlib import Path; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(300)']); "
        f"Path({str(pid_file)!r}).write_text(str(child.pid)); " + ("sys.exit(0)" if parent_exit else "time.sleep(300)")
    )
    return [sys.executable, "-c", script]


def _assert_descendant_stopped(pid_file: Path) -> None:
    assert pid_file.is_file()
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while psutil.pid_exists(pid):
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return
        except psutil.NoSuchProcess:
            return
        assert time.monotonic() < deadline, f"owned descendant is still alive: {pid}"
        time.sleep(0.02)


@pytest.mark.parametrize("reason", ["cancel", "timeout", "parent_exit"])
def test_owned_command_reaps_descendant_and_preserves_unrelated_process(tmp_path: Path, reason: str) -> None:
    pid_file = tmp_path / "child.pid"
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        command = _descendant_command(pid_file, parent_exit=reason == "parent_exit")
        if reason == "parent_exit":
            result = run_cancellable(command, capture_output=True, timeout=10, poll_interval_s=0.05)
            assert result.returncode == 0
        else:
            error = ProcessCancelledError if reason == "cancel" else subprocess.TimeoutExpired
            with pytest.raises(error):
                run_cancellable(
                    command,
                    cancel_check=pid_file.exists if reason == "cancel" else None,
                    capture_output=True,
                    timeout=2 if reason == "timeout" else 10,
                    poll_interval_s=0.05,
                )
        _assert_descendant_stopped(pid_file)
        assert unrelated.poll() is None
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=5)


def test_worker_cleanup_reaps_registered_command_without_competing_for_output(tmp_path: Path) -> None:
    from app.pipeline.lifecycle import cleanup_subprocesses

    pid_file = tmp_path / "child.pid"
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(
            run_cancellable, _descendant_command(pid_file), capture_output=True, timeout=15, poll_interval_s=0.05
        )
        try:
            deadline = time.monotonic() + 10
            while not pid_file.exists():
                assert not pending.done()
                assert time.monotonic() < deadline
                time.sleep(0.02)
            cleanup_subprocesses()
            result = pending.result(timeout=5)
            assert result.returncode != 0
            _assert_descendant_stopped(pid_file)
        finally:
            cleanup_subprocesses()


def test_cleanup_failure_keeps_ownership_and_cleans_other_commands() -> None:
    from app.analysis.transcription.process_tree import ProcessTree
    from app.pipeline import lifecycle

    class FailingTree(ProcessTree):
        fails = True

        def terminate(self) -> None:
            if self.fails:
                raise OSError("tree termination unavailable")
            super().terminate()

    first = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    second = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    failed = FailingTree(first.pid)
    lifecycle.track_subprocess(first, failed)
    lifecycle.track_subprocess(second, ProcessTree(second.pid))
    try:
        with pytest.raises(ExceptionGroup, match="进程树清理失败"):
            lifecycle.cleanup_subprocesses()
        assert first in lifecycle._subprocesses
        assert second not in lifecycle._subprocesses
        assert second.poll() is not None
    finally:
        failed.fails = False
        lifecycle.cleanup_subprocesses()
        first.wait(timeout=5)
        second.wait(timeout=5)
