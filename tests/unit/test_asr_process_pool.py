"""真实 spawn 进程监管回归；模型边界使用确定性替身，无下载及 GPU 依赖。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from multiprocessing.connection import Connection
from pathlib import Path
from typing import TYPE_CHECKING

import psutil
import pytest

from app.analysis.transcription.process_pool import ASRProcessPool, ASRShutdownError, ASRTimeoutError, RemoteASRError
from app.core.config import settings
from app.core.runtime_settings import effective_settings
from app.pipeline.lease import LeaseLostError

if TYPE_CHECKING:
    from app.analysis.transcription.process_pool import _Slot
    from app.pipeline.lease import TaskLease


def sdk_process(connection: Connection) -> None:
    """模拟外部同步 SDK，协议和 OS 生命周期由真实生产监管器执行。"""
    while True:
        try:
            request = json.loads(connection.recv_bytes())
        except EOFError:
            return
        context = request["context"]
        mode = context.get("mode", "success")
        pid_file = context.get("pid_file")
        if pid_file:
            Path(pid_file).write_text(str(os.getpid()))

        def send(kind: str, request_id: str = request["request_id"], **kwargs: object) -> None:
            connection.send_bytes(json.dumps({"kind": kind, "request_id": request_id, **kwargs}).encode())

        if mode == "descendant":
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
            Path(context["child_pid_file"]).write_text(str(child.pid))
        if mode in {"call", "iterator", "descendant"}:
            send("progress", stage="result_iteration" if mode == "iterator" else "inference_call", models=[])
            while True:
                time.sleep(0.1)
        if mode == "error":
            send("error", error="ValueError: model invalid\nOriginal model traceback", models=[])
        elif mode == "diagnostic":
            send(
                "diagnostic",
                level="WARNING",
                origin="fake_sdk:infer:42",
                message="model fallback at https://example.com/model?token=private-token",
            )
            send("result", result={"text": "ok"}, pid=os.getpid(), models=[])
        elif mode == "wrong_request":
            connection.send_bytes(json.dumps({"kind": "result", "request_id": "stale"}).encode())
        else:
            send("result", result={"text": "ok"}, pid=os.getpid(), models=[])


def request(**context: object) -> dict[str, object]:
    return {"settings": effective_settings().model_dump(mode="json"), "context": context}


def no_cancel() -> None:
    pass


def sdk_never_reads(connection: Connection) -> None:
    """启动握手由生产入口完成，然后模拟 SDK 在接收前卡住。"""
    time.sleep(300)


def test_ipc_thread_start_failure_reaps_model_process(monkeypatch: pytest.MonkeyPatch) -> None:
    original = threading.Thread.start
    pids: list[int] = []
    pool = ASRProcessPool(sdk_never_reads)

    def fail_ipc(thread: threading.Thread) -> None:
        if thread.name.startswith("asr-ipc-"):
            pids.append(int(thread.name.removeprefix("asr-ipc-")))
            raise RuntimeError("cannot start IPC thread")
        original(thread)

    monkeypatch.setattr(threading.Thread, "start", fail_ipc)
    try:
        with pytest.raises(RuntimeError, match="cannot start IPC thread"):
            pool.execute(request(), timeout_s=3, check_cancelled=no_cancel)
        assert pids and all(not psutil.pid_exists(pid) for pid in pids)
        assert pool._slots == [] and not pool._roles
    finally:
        pool.shutdown(0)


def sdk_partial_frame(connection: Connection) -> None:
    """POSIX 流管道只交付消息长度，模拟在整帧接收中挂死。"""
    import struct

    connection.recv_bytes()
    os.write(connection.fileno(), struct.pack("!i", 4096))
    time.sleep(300)


@pytest.mark.parametrize("cancel", [False, True])
def test_ipc_backpressure_cannot_block_supervision(cancel: bool) -> None:
    pool = ASRProcessPool(sdk_never_reads)
    cancelled = threading.Event()

    def check() -> None:
        if cancelled.is_set():
            raise LeaseLostError("cancelled blocked IPC")

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            running = executor.submit(
                pool.execute, request(large_prompt="x" * (4 * 1024 * 1024)), timeout_s=3, check_cancelled=check
            )
            time.sleep(1)
            if cancel:
                cancelled.set()
            with pytest.raises(LeaseLostError if cancel else ASRTimeoutError):
                running.result(timeout=12)
        assert pool._slots == [] and all(value == 0 for value in pool._roles.values())
    finally:
        pool.shutdown(0)


def test_partial_ipc_frame_cannot_block_supervision() -> None:
    # Windows 消息管道不暴露可写文件描述符；同平台用不读大请求验证无限等待边界。
    if os.name == "nt":
        target = sdk_never_reads
        payload = request(large_prompt="x" * (4 * 1024 * 1024))
    else:
        target = sdk_partial_frame
        payload = request()
    pool = ASRProcessPool(target)
    try:
        with pytest.raises(ASRTimeoutError):
            pool.execute(payload, timeout_s=3, check_cancelled=no_cancel)
        assert pool._slots == []
    finally:
        pool.shutdown(0)


def test_child_diagnostic_reaches_parent_with_context_and_redaction() -> None:
    from loguru import logger

    messages: list[str] = []
    sink = logger.add(lambda message: messages.append(str(message)))
    pool = ASRProcessPool(sdk_process)
    try:
        pool.execute(
            request(mode="diagnostic", task_id=159, lease_token="lease-a"), timeout_s=15, check_cancelled=no_cancel
        )
    finally:
        pool.shutdown(0)
        logger.remove(sink)
    report = "\n".join(messages)
    assert "asr_diagnostic" in report and "159" in report and "lease-a" in report
    assert "fake_sdk:infer:42" in report and "private-token" not in report


def wait_for_file(path: Path) -> int:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if path.exists() and path.read_text():
            return int(path.read_text())
        time.sleep(0.02)
    raise AssertionError(f"child did not start: {path}")


def test_success_reuses_process_and_shutdown_confirms_exit() -> None:
    pool = ASRProcessPool(sdk_process)
    try:
        first = pool.execute(request(), timeout_s=15, check_cancelled=no_cancel)
        second = pool.execute(request(), timeout_s=15, check_cancelled=no_cancel)
        assert first["pid"] == second["pid"]
        assert psutil.pid_exists(first["pid"])
    finally:
        pool.shutdown(0)
    assert not psutil.pid_exists(first["pid"])


@pytest.mark.parametrize("mode", ["call", "iterator"])
def test_blocked_sdk_times_out_and_replacement_process_works(mode: str) -> None:
    pool = ASRProcessPool(sdk_process)
    try:
        before = pool.execute(request(), timeout_s=15, check_cancelled=no_cancel)
        with pytest.raises(ASRTimeoutError, match="stage="):
            pool.execute(request(mode=mode), timeout_s=0.5, check_cancelled=no_cancel)
        assert not psutil.pid_exists(before["pid"])
        after = pool.execute(request(), timeout_s=15, check_cancelled=no_cancel)
        assert after["pid"] != before["pid"]
    finally:
        pool.shutdown(0)


def test_task_cancel_waits_for_real_process_cleanup_before_retry(
    temp_db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.analysis.transcription import process_pool
    from app.db.entities import SegmentTask, TaskStatus
    from app.db.session import get_session
    from app.pipeline import scheduler, task_worker
    from app.pipeline.lease import still_owns_lease
    from app.web.routers.tasks import cancel_task as api_cancel

    pool = ASRProcessPool(sdk_process)
    monkeypatch.setattr(process_pool, "get_process_pool", lambda: pool)
    cleanup_started, allow_cleanup = threading.Event(), threading.Event()
    kill = pool._kill

    def gated_kill(slot: _Slot) -> None:
        cleanup_started.set()
        assert allow_cleanup.wait(15)
        kill(slot)

    def infer(lease: TaskLease) -> None:
        def check() -> None:
            with get_session() as db:
                if not still_owns_lease(db, lease):
                    raise LeaseLostError("cancel requested")

        pool.execute(
            request(mode="call", pid_file=str(tmp_path / "pid"), task_id=lease.task_id, lease_token=lease.lease_token),
            timeout_s=30,
            check_cancelled=check,
        )

    monkeypatch.setattr(pool, "_kill", gated_kill)
    monkeypatch.setattr(scheduler, "run_transcribe", infer)
    with get_session() as db:
        task = SegmentTask(
            segment_id=1,
            session_id=1,
            stage=TaskStatus.TRANSCRIBING,
            claimed_by=scheduler._WORKER_ID,
            lease_token="cancel-proof",
        )
        db.add(task)
        db.flush()
        task_id = task.id
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(scheduler.execute_task, task_id, TaskStatus.TRANSCRIBING, "cancel-proof")
            try:
                pid = wait_for_file(tmp_path / "pid")
                assert api_cancel(task_id)["status"] == TaskStatus.CANCELLING
                assert cleanup_started.wait(5)
                assert psutil.pid_exists(pid)
                assert not task_worker.retry_task(task_id)
                with get_session() as db:
                    task = db.get(SegmentTask, task_id)
                    assert task.completed_at is None and task.lease_token == "cancel-proof"
            finally:
                allow_cleanup.set()
            future.result(timeout=15)
        assert not psutil.pid_exists(pid)
        assert not pool.has_active_task(task_id, "cancel-proof")
        with get_session() as db:
            task = db.get(SegmentTask, task_id)
            assert task.stage == TaskStatus.CANCELLED and task.completed_at is not None
            assert task.lease_token is task.claimed_by is None
        assert task_worker.retry_task(task_id)
        assert pool.execute(request(), timeout_s=15, check_cancelled=no_cancel)["pid"] != pid
    finally:
        allow_cleanup.set()
        pool.shutdown(0)


def test_cleanup_failure_does_not_report_cancelled_or_requeue(
    temp_db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.analysis.transcription import process_pool
    from app.db.entities import SegmentTask, TaskStatus
    from app.db.session import get_session
    from app.pipeline import scheduler, stale_recovery, task_worker
    from app.pipeline.lease import still_owns_lease

    pool = ASRProcessPool(sdk_process)
    monkeypatch.setattr(process_pool, "get_process_pool", lambda: pool)
    kill = pool._kill

    def fail_cleanup(slot: _Slot) -> None:
        raise RuntimeError("injected OS termination failure")

    def infer(lease: TaskLease) -> None:
        def check() -> None:
            with get_session() as db:
                if not still_owns_lease(db, lease):
                    raise LeaseLostError("cancel requested")

        pool.execute(
            request(mode="call", pid_file=str(tmp_path / "pid"), task_id=lease.task_id, lease_token=lease.lease_token),
            timeout_s=30,
            check_cancelled=check,
        )

    monkeypatch.setattr(pool, "_kill", fail_cleanup)
    monkeypatch.setattr(scheduler, "run_transcribe", infer)
    recovery_time = datetime.now(UTC) + timedelta(seconds=settings.stale_timeout_s + 60)
    monkeypatch.setattr(stale_recovery, "now_utc", lambda: recovery_time)
    with get_session() as db:
        task = SegmentTask(
            segment_id=1,
            session_id=1,
            stage=TaskStatus.TRANSCRIBING,
            claimed_by=scheduler._WORKER_ID,
            lease_token="cleanup-failure",
        )
        db.add(task)
        db.flush()
        task_id = task.id
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(scheduler.execute_task, task_id, TaskStatus.TRANSCRIBING, "cleanup-failure")
            try:
                pid = wait_for_file(tmp_path / "pid")
                assert task_worker.cancel_task(task_id)
                future.result(timeout=15)
                stale_recovery.recover_stale()
                with get_session() as db:
                    task = db.get(SegmentTask, task_id)
                    assert task.stage == TaskStatus.CANCELLING and task.completed_at is None
                    assert "IPC" in task.last_error
                assert psutil.pid_exists(pid)
                assert not task_worker.retry_task(task_id)
            finally:
                with pool._condition:
                    for slot in list(pool._slots):
                        kill(slot)
        assert not psutil.pid_exists(pid)
        stale_recovery.recover_stale()
        with get_session() as db:
            assert db.get(SegmentTask, task_id).stage == TaskStatus.CANCELLED
    finally:
        monkeypatch.setattr(pool, "_kill", kill)
        pool.shutdown(0)


def test_cancel_confirms_old_process_exit_before_retry(tmp_path: Path) -> None:
    cancelled = threading.Event()
    pool = ASRProcessPool(sdk_process)

    def check() -> None:
        if cancelled.is_set():
            raise LeaseLostError("old lease lost")

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                pool.execute, request(mode="call", pid_file=str(tmp_path / "pid")), timeout_s=30, check_cancelled=check
            )
            pid = wait_for_file(tmp_path / "pid")
            cancelled.set()
            with pytest.raises(LeaseLostError):
                future.result(timeout=15)
        assert not psutil.pid_exists(pid)
        assert pool.execute(request(), timeout_s=15, check_cancelled=no_cancel)["pid"] != pid
    finally:
        pool.shutdown(0)


def test_cancel_while_waiting_does_not_start_another_process(tmp_path: Path) -> None:
    pool = ASRProcessPool(sdk_process)
    cancelled = threading.Event()

    def check() -> None:
        if cancelled.is_set():
            raise LeaseLostError("cancelled before model slot")

    with ThreadPoolExecutor(max_workers=2) as executor:
        active = executor.submit(
            pool.execute,
            request(mode="call", pid_file=str(tmp_path / "active")),
            timeout_s=30,
            check_cancelled=no_cancel,
        )
        wait_for_file(tmp_path / "active")
        waiting = executor.submit(
            pool.execute, request(pid_file=str(tmp_path / "waiting")), timeout_s=30, check_cancelled=check
        )
        cancelled.set()
        with pytest.raises(LeaseLostError):
            waiting.result(timeout=10)
        assert not (tmp_path / "waiting").exists()
        pool.shutdown(0)
        with pytest.raises(ASRShutdownError):
            active.result(timeout=15)


def test_two_running_tasks_shutdown_reaps_both_and_descendants(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "asr_task_max_concurrency", 2)
    for role in ("primary", "auxiliary", "review", "fallback"):
        monkeypatch.setattr(settings, f"asr_{role}_max_concurrency", 2)
    pool = ASRProcessPool(sdk_process)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                pool.execute,
                request(
                    mode="descendant", pid_file=str(tmp_path / f"pid{i}"), child_pid_file=str(tmp_path / f"child{i}")
                ),
                timeout_s=30,
                check_cancelled=no_cancel,
            )
            for i in range(2)
        ]
        pids = [wait_for_file(tmp_path / name) for name in ("pid0", "child0", "pid1", "child1")]
        pool.shutdown(0.2)
        for future in futures:
            with pytest.raises(ASRShutdownError):
                future.result(timeout=15)
    for pid in pids:
        try:
            process = psutil.Process(pid)
            process.wait(timeout=5)
        except psutil.NoSuchProcess:
            pass
        assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE


def test_exception_preserves_diagnostics_and_returns_capacity() -> None:
    pool = ASRProcessPool(sdk_process)
    try:
        before = pool.execute(request(), timeout_s=15, check_cancelled=no_cancel)
        with pytest.raises(RemoteASRError, match="ValueError: model invalid"):
            pool.execute(request(mode="error"), timeout_s=15, check_cancelled=no_cancel)
        assert not psutil.pid_exists(before["pid"])
        after = pool.execute(request(), timeout_s=15, check_cancelled=no_cancel)
        assert after["result"]["text"] == "ok"
        assert after["pid"] != before["pid"]
    finally:
        pool.shutdown(0)


def test_wrong_request_result_is_rejected_and_slot_destroyed() -> None:
    pool = ASRProcessPool(sdk_process)
    try:
        first = pool.execute(request(), timeout_s=15, check_cancelled=no_cancel)
        with pytest.raises(RuntimeError, match="stale request"):
            pool.execute(request(mode="wrong_request"), timeout_s=15, check_cancelled=no_cancel)
        assert not psutil.pid_exists(first["pid"])
    finally:
        pool.shutdown(0)


def test_immediate_retry_waits_for_old_task_exit_with_two_slots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "asr_task_max_concurrency", 2)
    for role in ("primary", "auxiliary", "review", "fallback"):
        monkeypatch.setattr(settings, f"asr_{role}_max_concurrency", 2)
    pool = ASRProcessPool(sdk_process)
    cancelled = threading.Event()

    def check() -> None:
        if cancelled.is_set():
            raise LeaseLostError("cancelled old lease")

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            old = executor.submit(
                pool.execute,
                request(task_id=159, lease="old", mode="call", pid_file=str(tmp_path / "old")),
                timeout_s=30,
                check_cancelled=check,
            )
            pid = wait_for_file(tmp_path / "old")
            new = executor.submit(
                pool.execute,
                request(task_id=159, lease="new", pid_file=str(tmp_path / "new")),
                timeout_s=30,
                check_cancelled=no_cancel,
            )
            time.sleep(0.5)
            assert not (tmp_path / "new").exists()
            cancelled.set()
            result = new.result(timeout=15)
            assert result["pid"] != pid and not psutil.pid_exists(pid)
            with pytest.raises(LeaseLostError):
                old.result(timeout=10)
    finally:
        pool.shutdown(0)


def test_shutdown_reaps_parent_decoder_before_returning(tmp_path: Path) -> None:
    from app.analysis.transcription.audio_normalization import _run_normalization

    pool = ASRProcessPool(sdk_process)
    pid_file = tmp_path / "decoder"

    def decode() -> None:
        with pool.operation({"task_id": 159, "stage": "normalize"}):
            _run_normalization(
                [
                    sys.executable,
                    "-c",
                    "import os,sys,time; from pathlib import Path; Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(300)",
                    str(pid_file),
                ],
                pool.check_shutdown,
            )

    with ThreadPoolExecutor(max_workers=1) as executor:
        running = executor.submit(decode)
        pid = wait_for_file(pid_file)
        decoder = psutil.Process(pid)
        pool.shutdown(0)
        with pytest.raises(ASRShutdownError):
            running.result(timeout=5)
    # Windows 的已退出进程在最后一个句柄关闭前可能仍短暂保留 PID；等待退出句柄验证实际终止。
    decoder.wait(timeout=5)


def test_idle_configuration_change_discards_old_process(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = ASRProcessPool(sdk_process)
    try:
        first = pool.execute(request(), timeout_s=15, check_cancelled=no_cancel)
        monkeypatch.setattr(settings, "asr_auxiliary_keep_loaded", not settings.asr_auxiliary_keep_loaded)
        pool.cleanup_idle()
        assert not psutil.pid_exists(first["pid"])
        second = pool.execute(request(), timeout_s=15, check_cancelled=no_cancel)
        assert second["pid"] != first["pid"]
    finally:
        pool.shutdown(0)


def test_real_child_returns_silence_with_cpu_vad_without_model_download(
    pcm_wav: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.analysis.transcription.isolated import IsolatedPipeline

    monkeypatch.setattr(settings, "asr_primary", "whisper")
    pool = ASRProcessPool()
    try:
        result = IsolatedPipeline(pool=pool, context={"task_id": 159, "session_id": 1}).transcribe(str(pcm_wav))
        assert result.result_type == "no_speech"
        assert result.metadata["audio_evidence"]["duration_s"] == 8.336
        assert result.metadata["audio_evidence"]["sample_count"] == 133376
    finally:
        pool.shutdown(0)
