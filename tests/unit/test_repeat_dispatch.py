"""ASR 的真实重复检测分派与模型隔离边界。"""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from app.accelerators import dispatcher
from app.accelerators.python_fallback import speedups_round2 as reference
from app.analysis.transcription.quality import assess_transcript_quality, repair_local_decode_loop

if TYPE_CHECKING:
    from pytest import MonkeyPatch


@pytest.mark.parametrize("value", [None, 42, b"abc", [], ["a"] * 3])
@pytest.mark.parametrize("python_only", [False, True])
def test_repeat_dispatch_rejects_non_text(value: object, python_only: bool, monkeypatch: MonkeyPatch) -> None:
    if python_only:
        monkeypatch.setattr(dispatcher, "_repeat_backend", None)
    with pytest.raises(TypeError, match="text 必须是 str"):
        dispatcher.consecutive_repeat_details(value)  # type: ignore[arg-type]


def test_quality_and_repair_have_identical_native_and_python_results(monkeypatch: MonkeyPatch) -> None:
    rng = random.Random(804)
    prefix = "".join(chr(0x6000 + i) for i in range(800))
    suffix = "".join(chr(0x7000 + i) for i in range(800))
    texts = ["", "正常讲话！今天安排很好。", "真的真的", "哈哈" * 20]
    for size in (1, 3, 32, 33, 80, 257):
        unit = "".join(chr(0x4E00 + i) for i in range(size))
        for count in (2, 3, 4, 9):
            texts.extend([unit * count, prefix + (unit + "，") * count + suffix])
    texts.extend(prefix + ("Straßeİ汉字，" * count) + suffix for count in (3, 4, 8))
    texts.extend("".join(rng.choices("中abcßİ\0😀， ", k=size)) for size in (10, 100, 256, 257, 4096))
    results = [(assess_transcript_quality(text), repair_local_decode_loop(text)) for text in texts]
    monkeypatch.setattr(dispatcher, "_repeat_backend", None)
    assert dispatcher.get_repeat_backend() == "python"
    assert results == [(assess_transcript_quality(text), repair_local_decode_loop(text)) for text in texts]


def test_repeat_independent_of_cython_and_preserves_exact_long_period(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(dispatcher, "_cython_backend", None)
    unit = "".join(chr(0x4E00 + i) for i in range(1024))
    text = "前" + unit * 4 + "后"
    assert dispatcher.consecutive_repeat_details(text) == reference.consecutive_repeat_details(text) == (1, 1024, 4)


@pytest.mark.parametrize("without_native", [False, True])
def test_repeat_and_quality_do_not_load_models_or_change_devices(without_native: bool) -> None:
    script = r"""
import importlib.abc, json, os, sys
before = dict(os.environ)
blocked = {'torch', 'torchaudio', 'ctranslate2', 'faster_whisper', 'funasr', 'numpy'}
class BlockModels(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in blocked:
            raise AssertionError('unexpected model import: ' + fullname)
        if sys.argv[1] == '1' and fullname == 'app.accelerators._repeat_speedups':
            raise ImportError('simulate installation without native module')
sys.meta_path.insert(0, BlockModels())
from app.accelerators.dispatcher import consecutive_repeat_details, get_repeat_backend
from app.analysis.transcription.quality import assess_transcript_quality, repair_local_decode_loop
unit = ''.join(chr(0x4E00 + i) for i in range(80))
assert consecutive_repeat_details(unit * 4) == (0, 80, 4)
assert assess_transcript_quality(unit * 4).reason == 'degenerate_repetition'
repair_local_decode_loop('普通讲话' + unit * 4)
assert before == dict(os.environ)
assert not (blocked & sys.modules.keys())
if sys.argv[1] == '1':
    assert get_repeat_backend() == 'python'
print(json.dumps({'backend': get_repeat_backend(), 'unchanged': True}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(int(without_native))],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        env={**os.environ, "ASR_PRIMARY_DEVICE": "cuda", "CUDA_VISIBLE_DEVICES": "0"},
    )
    assert json.loads(completed.stdout)["unchanged"] is True
