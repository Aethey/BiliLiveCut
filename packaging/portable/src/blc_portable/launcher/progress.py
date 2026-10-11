"""模型准备的真实进度事件与实时子进程输出，不将下载完成等同于校验成功。"""

from __future__ import annotations

import codecs
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Mapping
from typing import Any, BinaryIO, TextIO

from blc_portable.diagnostics import redact_diagnostic

PREFIX = "BLC_MODEL_PROGRESS="
_PRINT_LOCK = threading.Lock()


def emit_progress(
    phase: str, label: str, completed: float | None = None, total: float | None = None, unit: str = "B"
) -> None:
    """发送 SDK 实测或显式阶段数据；未知总量保持 None。"""
    if os.environ.get("BLC_MODEL_PROGRESS") != "1":
        return
    payload = {"phase": phase, "label": redact_diagnostic(label), "completed": completed, "total": total, "unit": unit}
    with _PRINT_LOCK:
        print(PREFIX + json.dumps(payload, ensure_ascii=False), flush=True)


def hf_progress_class() -> type:
    """使用锁定 HF SDK 的公开 tqdm_class 接口，保留 SDK 计数语义。"""
    from tqdm import tqdm

    class ModelProgress(tqdm):
        def display(self, msg: str | None = None, pos: int | None = None) -> bool:
            """SDK 已节流的显示回调，字节与文件条目明确区分。"""
            label = str(self.desc or "Hugging Face")
            total = (
                None
                if any(word in label.lower() for word in ("incomplete", "transfer", "downloading bytes"))
                else self.total
            )
            emit_progress("download", label, self.n, total, self.unit)
            return True

    return ModelProgress


class ModelScopeProgress:
    """ModelScope 的断点重试会重复回调旧字节，只报告当前文件的活动。"""

    def __init__(self, filename: str, file_size: int) -> None:
        self.filename = filename
        self._last_emit = 0.0
        self._lock = threading.Lock()
        emit_progress("download", filename)

    def update(self, size: int) -> None:
        """SDK 没有可靠的去重累计值，不将回调量伪装成下载进度。"""
        with self._lock:
            now = time.monotonic()
            if now - self._last_emit >= 0.5:
                self._last_emit = now
                emit_progress("download", self.filename)

    def end(self) -> None:
        """回调结束不补齐未知或缺失字节，后续完整性验证独立进行。"""
        with self._lock:
            emit_progress("verify", self.filename + "：等待合并与校验")


class ProgressDisplay:
    """控制台单行进度；非交互日志输出节流行，停滞时仍报告已用时间。"""

    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream or sys.stdout
        self.started = time.monotonic()
        self.last_render = 0.0
        self.last_line = ""
        self.state: dict[str, Any] = {"phase": "prepare", "label": "读取本地模型与远程文件清单"}
        self.rates: dict[tuple[str, str], tuple[float, float, float | None]] = {}

    def update(self, state: Mapping[str, Any]) -> None:
        """仅接受有限非负计数，进度不改变模型准备结果。"""
        self.state = dict(state)
        now = time.monotonic()
        count = self._number(state.get("completed"))
        key = (str(state.get("label", "")), str(state.get("unit", "B")))
        if count is not None:
            previous = self.rates.get(key)
            rate = None
            if previous is not None and now > previous[0] and count >= previous[1]:
                rate = (count - previous[1]) / (now - previous[0])
            self.rates[key] = (now, count, rate)
        self.render(force=state.get("phase") != "download")

    @staticmethod
    def _number(value: object) -> float | None:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
            return float(value)
        return None

    def render(self, *, force: bool = False) -> None:
        """速率仅基于实际增量；无数据或超过五秒无增量时不外推 ETA。"""
        now = time.monotonic()
        if not force and now - self.last_render < 1:
            return
        self.last_render = now
        phase = str(self.state.get("phase", "prepare"))
        label = str(self.state.get("label", ""))
        count = self._number(self.state.get("completed"))
        total = self._number(self.state.get("total"))
        unit = str(self.state.get("unit", "B"))
        percent = min(count / total, 1) if count is not None and total and total >= count else None
        bar = (
            "=" * int(percent * 20) + " " * (20 - int(percent * 20)) if percent is not None else "       <=>          "
        )
        progress = f"{percent:.1%}" if percent is not None else "总量未知"
        rate_info = self.rates.get((label, unit))
        rate = rate_info[2] if rate_info and now - rate_info[0] <= 5 else None
        amount = (
            f" {count / 1024**2:.1f} MiB"
            if count is not None and unit == "B"
            else (f" {count:g} {unit}" if count is not None else "")
        )
        speed = f" {rate / 1024**2:.2f} MiB/s" if rate and unit == "B" else ""
        eta = f"{(total - count) / rate:.0f}s" if total and count is not None and total >= count and rate else "未知"
        names = {"prepare": "准备", "download": "下载", "verify": "校验", "install": "安装", "complete": "准备完成"}
        line = (
            f"  [{bar}] {names.get(phase, phase)} {label} | {progress}{amount}{speed}"
            f" | 已用 {now - self.started:.0f}s | 预计剩余 {eta}"
        )
        line = _safe_line(line)
        ending = "\r" if self.stream.isatty() else "\n"
        self.stream.write(line.ljust(len(self.last_line)) + ending)
        self.stream.flush()
        self.last_line = line

    def finish_line(self) -> None:
        """只结束显示行，不表示下载或验证成功。"""
        if self.stream.isatty() and self.last_line:
            self.stream.write("\n")
            self.stream.flush()


def _safe_line(line: str) -> str:
    # 禁止 SDK 输出的终端控制序列改变显示或在日志中藏匿诊断。
    line = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", line)
    return redact_diagnostic("".join(char for char in line if char >= " " or char == "\t"))


def run_provisioner(command: list[str], *, cwd: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """边读边显示双流，保留有界尾部及协议记录；Ctrl+C 等待子进程退出。"""
    display = ProgressDisplay()
    display.render(force=True)
    events: queue.Queue[tuple[str, str | None]] = queue.Queue(maxsize=128)
    stopped = threading.Event()
    read_errors: queue.Queue[BaseException] = queue.Queue(maxsize=2)
    tails: dict[str, deque[str]] = {"stdout": deque(maxlen=80), "stderr": deque(maxlen=80)}
    protocol: dict[str, str] = {}

    def enqueue(channel: str, line: str | None) -> None:
        while not stopped.is_set():
            try:
                events.put((channel, line), timeout=0.2)
                return
            except queue.Full:
                continue

    def reader(channel: str, pipe: BinaryIO) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        pending = ""
        oversized = False
        try:
            while chunk := pipe.read1(4096):
                for char in decoder.decode(chunk):
                    if char in "\r\n":
                        if pending or oversized:
                            enqueue(channel, "[超长诊断已省略]" if oversized else pending)
                        pending, oversized = "", False
                    elif not oversized:
                        pending += char
                        if len(pending) > 32768:
                            pending, oversized = "", True
            pending += decoder.decode(b"", final=True)
            if pending or oversized:
                enqueue(channel, "[超长诊断已省略]" if oversized else pending)
        except (OSError, ValueError) as exc:
            read_errors.put(exc)
            enqueue(channel, f"模型进度流读取失败: {type(exc).__name__}: {exc}")
        finally:
            enqueue(channel, None)

    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, env=env) as child:
        threads = [
            threading.Thread(target=reader, args=(channel, pipe), name=f"model-progress-{channel}", daemon=True)
            for channel, pipe in (("stdout", child.stdout), ("stderr", child.stderr))
        ]
        try:
            for thread in threads:
                thread.start()
            closed = 0
            while closed < 2:
                if not read_errors.empty():
                    raise RuntimeError("模型准备输出读取失败，无法确认子进程完整结果") from read_errors.get()
                try:
                    channel, line = events.get(timeout=1)
                except queue.Empty:
                    display.render()
                    continue
                if line is None:
                    closed += 1
                    continue
                if line.startswith(PREFIX):
                    try:
                        state = json.loads(line.removeprefix(PREFIX))
                    except json.JSONDecodeError:
                        state = None
                    if isinstance(state, dict):
                        display.update(state)
                        continue
                if line.startswith("BLC_PROVISION_RESULT=") and channel == "stdout":
                    # 完整机器协议只返回解析器，显示及异常边界另行脱敏。
                    protocol[channel] = line
                elif line.startswith("BLC_PROVISION_ROOT_EXCEPTION=") and channel == "stderr":
                    protocol[channel] = _safe_line(line)
                else:
                    line = _safe_line(line)
                    tails[channel].append(line[:2048])
                    display.finish_line()
                    print(line, file=sys.stderr if channel == "stderr" else sys.stdout, flush=True)
            returncode = child.wait()
            if not read_errors.empty():
                raise RuntimeError("模型准备输出读取失败，无法确认子进程完整结果") from read_errors.get()
        finally:
            stopped.set()
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
            for thread in threads:
                if thread.ident is not None:
                    thread.join(timeout=5)
                    if thread.is_alive():
                        raise RuntimeError("模型进度读取线程未退出")
            display.finish_line()
    return subprocess.CompletedProcess(
        command,
        returncode,
        "\n".join([*tails["stdout"], protocol.get("stdout", "")]),
        "\n".join([*tails["stderr"], protocol.get("stderr", "")]),
    )
