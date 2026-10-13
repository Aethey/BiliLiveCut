"""真实准备子进程的实时进度、双流、有界诊断与中断回归。"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import psutil
import pytest
from blc_portable.launcher import progress

if TYPE_CHECKING:
    from pytest import CaptureFixture, MonkeyPatch


def test_progress_is_visible_before_child_exit_and_utf8_is_not_split(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    acknowledged = tmp_path / "visible"

    class Output(io.StringIO):
        def write(self, text: str) -> int:
            if "正在下载测试模型" in text:
                acknowledged.write_text("seen", encoding="utf-8")
            return super().write(text)

    stream = Output()
    monkeypatch.setattr(sys, "stdout", stream)
    code = """
import json,sys,time
from pathlib import Path
data=('BLC_MODEL_PROGRESS='+json.dumps({'phase':'download','label':'正在下载测试模型','completed':10,'total':100},ensure_ascii=False)+'\\n').encode()
for byte in data:
 sys.stdout.buffer.write(bytes([byte])); sys.stdout.buffer.flush()
deadline=time.monotonic()+5
while not Path(sys.argv[1]).exists() and time.monotonic()<deadline: time.sleep(.02)
if not Path(sys.argv[1]).exists(): sys.exit(9)
print('BLC_PROVISION_RESULT={"source":"test"}',flush=True)
"""
    result = progress.run_provisioner(
        [sys.executable, "-u", "-c", code, str(acknowledged)], cwd=str(tmp_path), env=dict(os.environ)
    )
    assert result.returncode == 0
    assert "正在下载测试模型" in stream.getvalue()
    assert "�" not in stream.getvalue()
    assert 'BLC_PROVISION_RESULT={"source":"test"}' in result.stdout


def test_dual_streams_are_drained_and_failure_tail_is_bounded_and_redacted(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    code = """
import sys
for i in range(400):
 print('stdout-'+str(i)+'x'*400,flush=True)
 print('stderr-'+str(i)+'x'*400,file=sys.stderr,flush=True)
print('https://host.test/file?token=private123',file=sys.stderr,flush=True)
print('BLC_PROVISION_ROOT_EXCEPTION=ValueError: broken model',file=sys.stderr,flush=True)
sys.exit(3)
"""
    result = progress.run_provisioner([sys.executable, "-u", "-c", code], cwd=str(tmp_path), env=dict(os.environ))
    output = capsys.readouterr()
    assert result.returncode == 3
    assert len(result.stdout.splitlines()) <= 81 and len(result.stderr.splitlines()) <= 81
    assert "stdout-399" in result.stdout and "stderr-399" in result.stderr
    assert "BLC_PROVISION_ROOT_EXCEPTION=ValueError: broken model" in result.stderr
    assert "private123" not in output.err + result.stderr
    assert "<host:host.test>" in output.err
    assert "准备完成" not in output.out


def test_display_only_estimates_from_reliable_count_and_fresh_rate(monkeypatch: MonkeyPatch) -> None:
    now = [100.0]
    monkeypatch.setattr(progress.time, "monotonic", lambda: now[0])
    stream = io.StringIO()
    display = progress.ProgressDisplay(stream)
    display.update({"phase": "download", "label": "file", "completed": 10, "total": 100})
    assert "10.0%" in stream.getvalue() and "预计剩余 未知" in stream.getvalue()
    now[0] += 2
    display.update({"phase": "download", "label": "file", "completed": 30, "total": 100})
    assert "预计剩余 7s" in stream.getvalue()
    now[0] += 6
    display.render()
    assert "预计剩余 未知" in stream.getvalue().splitlines()[-1]
    display.update({"phase": "verify", "label": "checksum"})
    assert "总量未知" in stream.getvalue().splitlines()[-1]
    assert "100.0%" not in stream.getvalue()


def test_sdk_events_work_in_pipe_and_do_not_use_estimated_or_duplicate_totals(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    monkeypatch.setenv("BLC_MODEL_PROGRESS", "1")
    klass = progress.hf_progress_class()
    with klass(desc="Reconstructing", total=100, unit="B", mininterval=0) as bar:
        bar.update(20)
    with klass(desc="Downloading bytes", total=100, unit="B", mininterval=0) as bar:
        bar.update(20)
    callback = progress.ModelScopeProgress("model.bin", 100)
    callback.update(80)
    callback.update(80)  # 重试报告相同断点，不能变成 160%。
    callback.end()
    states = [
        json.loads(line.removeprefix(progress.PREFIX))
        for line in capsys.readouterr().out.splitlines()
        if line.startswith(progress.PREFIX)
    ]
    assert any(item["completed"] == 20 and item["total"] == 100 for item in states)
    assert all(item["total"] is None for item in states if item["label"] == "Downloading bytes")
    assert all(item["completed"] is None and item["total"] is None for item in states if "model.bin" in item["label"])
    assert not any(item["phase"] == "complete" for item in states)


def test_interrupted_display_stops_real_child(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    child_processes: list[psutil.Process] = []

    class InterruptedOutput(io.StringIO):
        def write(self, text: str) -> int:
            if text.startswith("READY:"):
                child_processes.append(psutil.Process(int(text.removeprefix("READY:"))))
                raise KeyboardInterrupt
            return super().write(text)

    monkeypatch.setattr(sys, "stdout", InterruptedOutput())
    code = "import os,time; print('READY:'+str(os.getpid()),flush=True); time.sleep(30)"
    with pytest.raises(KeyboardInterrupt):
        progress.run_provisioner([sys.executable, "-u", "-c", code], cwd=str(tmp_path), env=dict(os.environ))
    assert len(child_processes) == 1
    child_processes[0].wait(timeout=5)


def test_sdk_progress_gc_during_thread_start_cannot_deadlock(tmp_path: Path) -> None:
    """在真实子进程强制重现 tqdm 启动线程时的 GC/共享锁交叉等待。"""
    code = r"""
import faulthandler,gc,io,json,os,threading
from tqdm import tqdm
from blc_portable.launcher import progress
faulthandler.dump_traceback_later(2)
os.environ['BLC_MODEL_PROGRESS']='1'
tqdm.get_lock()
original_interval=tqdm.monitor_interval
class OrphanBar(tqdm):
    monitor_interval=0
gc.disable()
orphan=OrphanBar(total=1,file=io.StringIO())
orphan.cycle=orphan
del orphan
original_start=threading.Thread._set_tstate_lock
def collect_during_start(self):
    if self.name=='tqdm_monitor': gc.collect()
    original_start(self)
threading.Thread._set_tstate_lock=collect_during_start
klass=progress.hf_progress_class()
with klass(desc='Reconstructing',total=1000,unit='B',mininterval=0) as bar:
    bar.update(900)
    bar.update(1)
    assert bar.n==901
assert tqdm.monitor_interval==original_interval
gc.collect()
faulthandler.cancel_dump_traceback_later()
print('GC_PROGRESS_OK',flush=True)
"""
    result = subprocess.run(
        [sys.executable, "-u", "-c", code],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(Path(progress.__file__).resolve().parents[2])},
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=5,
        check=True,
    )
    assert "GC_PROGRESS_OK" in result.stdout
    states = [
        json.loads(line.removeprefix(progress.PREFIX))
        for line in result.stdout.splitlines()
        if line.startswith(progress.PREFIX)
    ]
    assert any(item["completed"] == 901 and item["total"] == 1000 for item in states)


def test_sdk_progress_reports_small_updates_after_burst(monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]) -> None:
    monkeypatch.setenv("BLC_MODEL_PROGRESS", "1")
    klass = progress.hf_progress_class()
    bar = klass(desc="Reconstructing", total=1000, unit="B", mininterval=0.1)
    clock = [bar.last_print_t]
    bar._time = lambda: clock[0]
    try:
        capsys.readouterr()
        clock[0] += 1
        bar.update(900)
        capsys.readouterr()
        clock[0] += 2
        bar.update(1)
        states = [
            json.loads(line.removeprefix(progress.PREFIX))
            for line in capsys.readouterr().out.splitlines()
            if line.startswith(progress.PREFIX)
        ]
        assert any(item["completed"] == 901 and item["total"] == 1000 for item in states)
    finally:
        bar.close()
