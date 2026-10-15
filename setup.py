"""BiliLiveCut 构建配置 — 含 C 加速模块编译 (V0.1.13.2)."""

import os
import sys

from setuptools import Extension, find_packages, setup
from setuptools.command.build_ext import build_ext


class NativeBuildExt(build_ext):
    """按各源文件语言编译混合扩展，避免把 C++ 标准参数传给 C 编译器。"""

    def build_extension(self, ext: Extension) -> None:
        """单独生成混合扩展中的 C 对象，再交给原构建流程链接 C++ 扩展。"""
        c_sources = [source for source in ext.sources if source.endswith(".c")] if ext.language == "c++" else []
        if not c_sources:
            super().build_extension(ext)
            return
        sources, objects, depends = ext.sources, ext.extra_objects, ext.depends
        c_args = [arg for arg in ext.extra_compile_args if not arg.startswith(("-std=c++", "/std:c++"))]
        macros = ext.define_macros + [(name,) for name in ext.undef_macros]
        c_objects = self.compiler.compile(
            c_sources,
            output_dir=self.build_temp,
            macros=macros,
            include_dirs=ext.include_dirs,
            debug=self.debug,
            extra_postargs=c_args,
            depends=ext.depends,
        )
        try:
            ext.sources = [source for source in sources if source not in c_sources]
            ext.extra_objects = list(objects or []) + c_objects
            ext.depends = list(depends or []) + c_sources + c_objects
            super().build_extension(ext)
        finally:
            ext.sources, ext.extra_objects, ext.depends = sources, objects, depends


_skip_extensions = os.environ.get("BLC_SKIP_C_EXTENSIONS", "").strip().lower() in ("1", "true", "yes")
_extensions = []

if not _skip_extensions:
    # 第一轮: C 直接编译 (Aho-Corasick + 余弦相似度 + bigram)。
    _c_speedups = Extension(
        "app.accelerators._c_speedups",
        sources=["tools/native/c/_c_speedups.c"],
        extra_compile_args=(
            ["/O2", "/fp:precise", "/utf-8"] if sys.platform == "win32" else ["-O3", "-ffp-contract=off"]
        ),
        extra_link_args=(["/Brepro"] if sys.platform == "win32" else []),
    )
    _extensions.append(_c_speedups)

    # Cython: 聚类、音频数值热点、滚动基线、弹幕基线与 SRT。
    try:
        from Cython.Build import cythonize  # noqa: F401

        _r2 = Extension(
            "app.accelerators._cython_speedups",
            sources=["tools/native/cython/_cython_speedups.pyx"],
            extra_compile_args=(["/O2", "/utf-8"] if sys.platform == "win32" else ["-O3", "-ffp-contract=off"]),
            extra_link_args=(["/Brepro"] if sys.platform == "win32" else []),
        )
        _extensions.append(_r2)

        _repeat = Extension(
            "app.accelerators._repeat_speedups",
            sources=[
                "tools/native/cython/_repeat_speedups.pyx",
                "tools/native/repeat/repeat.cpp",
                "tools/native/repeat/vendor/libsais/src/libsais.c",
            ],
            include_dirs=["tools/native/repeat", "tools/native/repeat/vendor/libsais/include"],
            depends=["tools/native/repeat/repeat.hpp", "tools/native/repeat/vendor/libsais/include/libsais.h"],
            language="c++",
            extra_compile_args=(["/O2", "/utf-8", "/std:c++17"] if sys.platform == "win32" else ["-O3", "-std=c++17"]),
            extra_link_args=(["/Brepro"] if sys.platform == "win32" else []),
        )
        _extensions.append(_repeat)
    except ImportError:
        # Cython 未安装：跳过编译，由加速分派层使用 Python 参考实现。
        pass

setup(
    packages=find_packages(include=["app", "app.*", "config"]),
    ext_modules=_extensions,
    cmdclass={"build_ext": NativeBuildExt},
    include_package_data=True,
)
