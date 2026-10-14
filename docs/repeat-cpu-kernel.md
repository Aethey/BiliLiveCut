# 任意周期重复检测 CPU 内核

独立接口 `app.accelerators._repeat_speedups.consecutive_repeat_details(text)` 接收 Python 字符串，
返回 `(start, unit, repeats)`。只有连续三次以上完全相同的重复才参与选择；先最大化覆盖字符数，
再选择最短周期、最早起点。没有符合项返回 `(0, 0, 0)`。
位置使用 Unicode 码点，NUL、非 BMP 字符和孤立代理码点均按 Python 字符串语义处理。
内核不承担大小写折叠、标点清理、文本修改或模型判定。

## 算法与边界

- C++17 实现位于 `tools/native/repeat/repeat.cpp`，薄 Cython 绑定只负责类型检查、
  复制码点和结果转换。完成自有缓冲区复制后才释放 GIL；不借用可变 Python 内存。
- 256 码点以内检查全部可能周期，最多进行 85 次线性扫描。固定上限保证这一小输入路径
  最多执行 85N 次比较，没有限制可检测的周期长度。
- 大输入先用线性前缀函数处理覆盖整串的最短周期；未命中时用四趟字节基数排序压缩字符表。
- 使用两份 SA-IS 后缀数组及 Kasai LCP，分别服务正向和反向最长公共延伸查询。
  相反字符顺序同时反转结束哨兵，因此可以反转既有后缀排名，无需第三份后缀数组。
- 根据 [Bannai 等人的 Runs 算法，第四节](https://arxiv.org/abs/1406.0263)，
  每个位置在两种顺序下各产生一个最长 Lyndon 候选；用常数时间的前后延伸取得重复区间。
  全部候选只保留最优结果，不展开所有重复子串，也不使用概率哈希。
- RMQ 的块长为 `floor(log2 N)`。块内使用单调栈位掩码；稀疏表只存约 N/logN 个块的最小值，
  所以预处理总量是 O(N)，查询 O(1)，没有在 N 个元素上建立 O(N logN) 的稀疏表。
- 总计 O(N) 时间、O(N) 附加空间，依据为固定宽度整数码点和字长运算模型。
  拒绝超过 2,147,483,646 码点的输入，避免有符号索引溢出；分配失败向 Python 抛出错误。
  内核仅操作 CPU 内存，没有网络、文件、进程、模型、CUDA 或线程池操作。

## 第三方来源

`vendor/libsais` 固定为上游 [v2.10.4](https://github.com/IlyaGrebnov/libsais/tree/ce90878d784b5ff7d019300535675e4a2e22aae0)，
采用该提交实际附带的 Apache-2.0 License，版权头与许可证均保留。OpenMP 不参与编译。
`UPSTREAM.json` 记录上游提交、Git blob、原始 SHA-256 与纳入仓库后的 SHA-256。
唯一局部修改是删除行尾 ASCII 空格/制表符；许可证、代码语义和版本未变。
Windows/Linux 的检出统一使用 LF，单元测试核对纳入仓库的全部文件哈希。
项目自身仍使用 MIT；wheel 与 Docker 镜像同时包含 libsais 的许可证。

## 验证和基准

- `python -m pytest tests/unit/test_repeat_kernel.py`：独立穷举、长算法路径穷举、Unicode 全码点、
  任意周期、平局、边界和可重入调用。
- Linux GCC 下 `python tools/native/repeat/check_native.py`：AddressSanitizer、LeakSanitizer、UBSan，
  独立 C++ 穷举对照、空指针/长度溢出拒绝、分配失败和输入不可变检查。
- `python scripts/benchmark_repeat_kernel.py --baseline <旧Cython扩展> --output <结果.json>`：
  对照旧原生模块，预热后记录五组中位数；输入生成不计入时间。

2026-10-14 同一台机器的 Windows Python 3.12.14 与 Docker Linux Python 3.12 测量如下。
原始耗时、平台和二进制 SHA-256 保存在 `docs/benchmarks/pr80-repeat-*.json`。
12 类样本在两个环境中均未出现减速；结果描述已测样本，不能保证所有硬件和所有输入的倍率。

| 样本 | Windows 倍率 | Linux 倍率 |
| --- | ---: | ---: |
| 普通短句 | 18.58 | 48.99 |
| 短句循环 | 14.42 | 18.28 |
| 256 / 257 字符边界 | 6.01 / 5.61 | 6.95 / 9.09 |
| 128K 字符整串周期 | 26.03 | 58.46 |
| 128K 字符局部周期 | 1.46 | 2.02 |
| 128K 相同字符 | 34.23 | 92.98 |
| 16K 到 128K 随机文本 | 10.13–12.86 | 10.45–13.17 |

随机文本长度从 16K 翻倍到 128K 时，新内核 Windows 耗时约 2.64、5.10、10.30、21.96 ms；
Linux 约 2.56、5.50、10.26、21.16 ms，与线性增长相符。
