# ASR 重复检测接入与分发

重复检测现在由 `app.accelerators.dispatcher.consecutive_repeat_details` 调用独立 C++17 内核，`get_repeat_backend()` 返回 `C++` 或 `python`。此入口只接收字符串，返回起点、最小周期、重复次数；覆盖长度优先、周期较短优先、起点较早优先。周期不限制为 32 字符。

现有 ASR 质量阈值、局部循环保留两次的修复行为、原文索引、备用 ASR 和 CUDA 配置不变。内核不导入模型或张量库，不分配 GPU 内存，不设置设备，不启动线程；超过 96 码点时在持有独立输入副本后释放 GIL；更短的有界调用使用栈缓冲区，避免分配及 GIL 交接。原 Cython 模块中的重复算法已移除，其他数值热点继续使用该模块。

普通源码安装和 wheel 默认编译新模块。显式 `BLC_SKIP_C_EXTENSIONS=1` 的安装使用同语义 Python 参考算法（O(N log N)），不依赖本地编译工具；C++ 算法的时间和辅助空间为 O(N)。短输入使用有固定上界的精确扫描。算法和测量边界见 [独立内核说明](repeat-cpu-kernel.md)。

Windows Payload 必须新生成匹配当前 Python ABI 的 C、Cython、C++ 和 Rust 模块；构建成功但缺失文件、只留下旧文件、包含本机路径均拒绝打包。Payload 随附 `app/accelerators/licenses/libsais-LICENSE.txt`，wheel 和 Docker 也保留第三方许可证。Docker CPU 镜像原有设备策略不变。CI 分别验证有原生模块和无原生模块的实际入口；发布验收同时核对文件和加载后端。

回归覆盖任意周期、Unicode 大小写展开、局部循环修复、正常强调、两个后端的质量结果一致，以及禁止加载模型库时仍能运行。每项加速结论只针对记录中的平台和样本；完整转写速度仍取决于模型推理和输入，不以局部内核倍率替代端到端测量。
