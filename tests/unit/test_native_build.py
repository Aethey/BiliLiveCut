from __future__ import annotations

import runpy
from pathlib import Path

import pytest
import setuptools
import yaml
from setuptools import Distribution, Extension
from setuptools._distutils.compilers.C.unix import Compiler

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("standard", ["-std=c++17", "/std:c++17"])
def test_mixed_extension_uses_language_specific_compiler_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, standard: str
) -> None:
    # 在编译器进程边界记录真实 setuptools 调用，不模拟扩展构建逻辑。
    monkeypatch.setattr(setuptools, "setup", lambda **kwargs: None)
    native_build = runpy.run_path(str(ROOT / "setup.py"))["NativeBuildExt"]
    c_source, cpp_source = tmp_path / "helper.c", tmp_path / "module.cpp"
    c_source.write_text("int helper(void) { return 42; }", encoding="utf-8")
    cpp_source.write_text('extern "C" int helper(void);', encoding="utf-8")
    extension = Extension("fixture", sources=[str(c_source), str(cpp_source)], language="c++")
    extension.extra_compile_args = ["-O3", standard]
    sources, objects, depends = extension.sources, extension.extra_objects, extension.depends
    command = native_build(Distribution({"ext_modules": [extension]}))
    command.ensure_finalized()
    command.build_temp = str(tmp_path / "objects")
    command.build_lib = str(tmp_path / "output")
    command.compiler = Compiler(force=True)
    command.compiler.set_executables(
        compiler_so="clang", compiler_cxx="clang++", linker_so_cxx="clang++ -shared", linker_exe_cxx="clang++"
    )
    invocations: list[list[str]] = []

    def spawn(arguments: list[str], **kwargs: object) -> None:
        invocations.append(arguments)

    monkeypatch.setattr(command.compiler, "spawn", spawn)
    command.build_extension(extension)
    c_call = next(call for call in invocations if "-c" in call and str(c_source) in call)
    cpp_call = next(call for call in invocations if "-c" in call and str(cpp_source) in call)
    assert any("-shared" in call for call in invocations), invocations
    link_call = next(call for call in invocations if "-shared" in call)
    assert "-O3" in c_call and standard not in c_call
    assert standard in cpp_call
    assert any(argument.endswith("helper.o") for argument in link_call)
    assert any(argument.endswith("module.o") for argument in link_call)
    assert extension.sources is sources and extension.extra_objects is objects and extension.depends is depends


def test_macos_requires_compiled_native_extensions_before_full_tests() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["test-macos"]
    assert job["env"]["BLC_SKIP_C_EXTENSIONS"] == "0"
    assert job["env"]["CC"] == "clang" and job["env"]["CXX"] == "clang++"
    commands = [step.get("run", "") for step in job["steps"]]
    native = next(
        index for index, text in enumerate(commands) if "import _c_speedups, _cython_speedups, _repeat_speedups" in text
    )
    assert native < commands.index("python scripts/run_coverage.py")
