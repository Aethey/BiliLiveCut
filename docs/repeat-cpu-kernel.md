# 任意周期重复检测 CPU 内核

独立接口 `app.accelerators._repeat_speedups.consecutive_repeat_details(text)` 接收 Python 字符串，
返回 `(start, unit, repeats)`。只有连续三次以上完全相同的重复才参与选择；先最大化覆盖字符数，
再选择最短周期、最早起点。没有符合项返回 `(0, 0, 0)`。
位置使用 Unicode 码点，NUL、非 BMP 字符和孤立代理码点均按 Python 字符串语义处理。
内核不承担大小写折叠、标点清理、文本修改或模型判定。

## 算法与边界

- C++17 实现位于 `tools/native/repeat/repeat.cpp`。薄 Cython 绑定只复制码点与转换结果；
  最多 96 码点使用栈缓冲区，在有界短调用内保留 GIL，避免分配和交接开销；更长输入在拥有
  独立副本后释放 GIL，不借用可变 Python 内存。
- 96 码点以内扫描全部周期，最多 32N 次比较。97～512 码点直接比较后缀并延伸 Lyndon 候选，
  使用固定栈；该有界路径最多 O(N²) 次比较，但 N≤512，因此不改变整体 O(N) 上界。
- 96 码点以上先以线性前缀函数处理整串最短周期。大输入按最多四趟字节基数排序压缩字符表，
  跳过全零高字节。少于三次出现的字符不可能属于三次连续重复，因此可作为精确分隔符；
  只分段一次，子段不再递归分段，总长度和工作量仍受 N 约束。
- 先构建正向 SA-IS 后缀数组与 Kasai LCP。周期 p 的三次重复必有长度至少 2p 的公共前缀；
  相邻后缀最大 LCP 是所有后缀对的上界。若此上界只允许 p≤32，则扫描全部可行周期，
  不建立反向数组和 RMQ；这不是限制可检测的周期。
- 其他输入才建立反向后缀数组和两份 RMQ。压缩字符表不超过 256 时使用 libsais 字节入口，
  否则使用整数入口；保留 Unicode 排序与所有码点，不截断原始字符。相反字符顺序同时反转
  结束哨兵，因此可反转既有后缀排名，无需第三份后缀数组。
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
- `python scripts/benchmark_repeat_kernel.py --baseline <main原生扩展> --baseline-commit <完整SHA> --output <结果.json> --check-no-regression`：
  强制传入可追溯 main 基线。逐样本核对输出相同后，预热、校准到每组约 20ms，记录七组中位数；
  任一等价样本变慢即失败，输入生成不计入耗时。

2026-10-15 在同一机器验证 Windows Python 3.12 与 Docker Linux Python 3.12。
基线固定为 main `e0a1bfe9cf3ad0560f5c8f1115ed9da11772f9d1`，分别使用相同平台和优化级别重新编译。
原始耗时、返回值、平台、基线提交及二进制 SHA-256 保存在 `docs/benchmarks/pr80-repeat-*.json`。
16 组等价输入在两个平台均通过门禁。旧 main 只检测最多 32 字符周期，因此 33/64/128/257
周期的新增能力单独验证正确性并记录耗时，不将不同结果用作加速倍率。
这些结果描述实测样本，不承诺所有输入、硬件和调度状态的固定倍率。

| 样本 | Windows 倍率 | Linux 倍率 |
| --- | ---: | ---: |
| `empty` | 1.32 | 1.39 |
| `short-normal` | 3.00 | 1.25 |
| `short-loop` | 10.56 | 6.30 |
| `noise-256` | 6.03 | 2.35 |
| `noise-257` | 5.12 | 2.41 |
| `noise-1000` | 3.15 | 2.56 |
| `noise-16000` | 1.39 | 1.32 |
| `noise-32000` | 1.35 | 1.45 |
| `noise-64000` | 1.31 | 1.43 |
| `noise-128000` | 1.24 | 1.25 |
| `noise-511` | 5.85 | 4.71 |
| `noise-512` | 6.01 | 2.48 |
| `noise-513` | 2.95 | 1.81 |
| `long-whole-period-16` | 15.83 | 8.64 |
| `long-local-period-16` | 5.84 | 3.10 |
| `long-uniform` | 17.10 | 30.51 |
