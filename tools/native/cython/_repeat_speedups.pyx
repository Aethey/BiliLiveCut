"""精确任意周期重复检测的薄绑定；仅复制 Unicode 码点，不接触设备。"""

from libc.stdint cimport uint32_t, int32_t
from libc.stddef cimport size_t
from libcpp.vector cimport vector
from cpython.unicode cimport PyUnicode_READ_CHAR

cdef extern from "repeat.hpp" namespace "blc_repeat":
    cdef cppclass Result:
        int32_t start
        int32_t unit
        int32_t repeats
    Result find_repeat(const uint32_t* text, size_t size) except + nogil


def consecutive_repeat_details(str text not None) -> tuple[int, int, int]:
    """返回覆盖最长、周期最短、起点最早的三次以上连续重复。"""
    cdef Py_ssize_t size = len(text), index
    cdef vector[uint32_t] symbols
    cdef Result result
    if size < 3:
        return 0, 0, 0
    if size > 2147483646:
        raise OverflowError("repeat input exceeds signed 32-bit index limit")
    symbols.resize(size)
    for index in range(size):
        symbols[index] = PyUnicode_READ_CHAR(text, index)
    with nogil:
        result = find_repeat(symbols.data(), <size_t>size)
    return result.start, result.unit, result.repeats
