# 技术参考

这里描述当前实现的算法与模块边界。业务操作见[功能指南](../features/README.md)，构建与检查命令见[开发与扩展](../development/README.md)。

## 高光分析

[热点检测器](hotspot-detector.md)说明事件生命周期、信号融合、语义证据与成片评分，以及缺少 ASR 时的降级行为。

## 原生加速

- [模块与调用边界](native-acceleration.md)：C、Cython、C++ 和 Rust 的职责、构建与 Python 参考路径。
- [任意周期重复检测](repeat-cpu-kernel.md)：输入输出、复杂度、Unicode 语义、ASR 接入和可追溯基准。

基准结果只覆盖记录中的平台和样本，不能替代完整录制、模型推理或渲染链路的现场验收。
