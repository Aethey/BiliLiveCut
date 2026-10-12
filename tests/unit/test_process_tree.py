"""跨平台验证 POSIX 进程树的终止所有权与重复清理。"""

from __future__ import annotations

import errno
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.analysis.transcription import process_tree


@pytest.fixture()
def posix_signals(monkeypatch: pytest.MonkeyPatch) -> tuple[Mock, Mock]:
    killpg = Mock()
    kill = Mock()
    monkeypatch.setattr(process_tree, "os", SimpleNamespace(name="posix", killpg=killpg, kill=kill))
    monkeypatch.setattr(process_tree, "signal", SimpleNamespace(SIGKILL=9))
    return killpg, kill


def test_repeated_cleanup_does_not_signal_terminated_group(posix_signals: tuple[Mock, Mock]) -> None:
    killpg, kill = posix_signals
    # Darwin 对只剩僵尸成员的组可能返回 EPERM；不能再次发送已经成功的终止信号。
    killpg.side_effect = [None, PermissionError(errno.EPERM, "no signalable group members")]
    tree = process_tree.ProcessTree(1234)
    tree.terminate()
    tree.terminate()
    killpg.assert_called_once_with(1234, 9)
    kill.assert_not_called()


@pytest.mark.parametrize("parent_gone", [False, True])
def test_missing_group_terminates_starting_process_only_once(
    posix_signals: tuple[Mock, Mock], parent_gone: bool
) -> None:
    killpg, kill = posix_signals
    killpg.side_effect = ProcessLookupError(errno.ESRCH, "group not created")
    if parent_gone:
        kill.side_effect = ProcessLookupError(errno.ESRCH, "process exited")
    tree = process_tree.ProcessTree(1234)
    tree.terminate()
    tree.terminate()
    killpg.assert_called_once_with(1234, 9)
    kill.assert_called_once_with(1234, 9)


def test_real_group_permission_failure_remains_visible_and_retryable(posix_signals: tuple[Mock, Mock]) -> None:
    killpg, kill = posix_signals
    denied = PermissionError(errno.EPERM, "live group permission denied")
    killpg.side_effect = [denied, None]
    tree = process_tree.ProcessTree(1234)
    with pytest.raises(PermissionError) as raised:
        tree.terminate()
    assert raised.value is denied
    kill.assert_not_called()
    tree.terminate()
    tree.terminate()
    assert killpg.call_count == 2


def test_starting_process_permission_failure_remains_retryable(posix_signals: tuple[Mock, Mock]) -> None:
    killpg, kill = posix_signals
    killpg.side_effect = ProcessLookupError(errno.ESRCH, "group not created")
    denied = PermissionError(errno.EPERM, "process permission denied")
    kill.side_effect = [denied, None]
    tree = process_tree.ProcessTree(1234)
    with pytest.raises(PermissionError) as raised:
        tree.terminate()
    assert raised.value is denied
    tree.terminate()
    tree.terminate()
    assert kill.call_count == 2


def test_concurrent_cleanup_sends_one_group_termination(posix_signals: tuple[Mock, Mock]) -> None:
    killpg, kill = posix_signals
    tree = process_tree.ProcessTree(1234)
    with ThreadPoolExecutor(max_workers=4) as executor:
        pending = [executor.submit(tree.terminate) for _ in range(16)]
        for result in pending:
            result.result(timeout=5)
    killpg.assert_called_once_with(1234, 9)
    kill.assert_not_called()
