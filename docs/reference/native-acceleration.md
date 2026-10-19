# 原生加速模块

适用版本：`0.1.19.2-alpha`。

Rust 构建通过 `CARGO_ENCODED_RUSTFLAGS` 传递路径映射，保留显式编译选项，避免用户目录或 Cargo 缓存路径嵌入原生模块；含空格的路径保持完整。Payload 在收录原生模块前检查 UTF-8/UTF-16 本机构建路径，发现残留则拒绝构建。

自定义编译选项请通过 `RUSTFLAGS` 或 `CARGO_ENCODED_RUSTFLAGS` 显式提供。若仅在 Cargo 的 `build` / `target` 配置（包括对应环境变量）设置 `rustflags`，或配置使用 `include`，构建会明确报错并要求将完整选项传入上述变量，以免路径映射覆盖原有链接或目标平台参数。相对 `CARGO_HOME` 按 Rust 源码目录解析。

当前业务架构与安装入口见[项目 README](../../README.md)和 [Portable 说明](../../packaging/portable/README.md)；版本变更见 [Changelog 归档索引](../changelog/CHANGELOG_INDEX.md)。

BiliLiveCut 只保留当前 `app.accelerators` 原生接口。业务代码统一调用
`app.accelerators.dispatcher`，不直接依赖编译扩展；旧的 `app.analysis` 原生
模块路径和函数别名均不再提供。

## 当前已接入的原生化清单

| 热点 | 实现 | 接入结果 | 原因 |
|---|---|---|---|
| 并查集与簇内平均相似度 | Rust | 独立 CPU 内核 | 保留连接顺序、根编号、簇顺序与浮点累加顺序；计算期间释放 GIL |
| 文本词项、实体与新颖度 | Cython | 独立 CPU 字符内核 | 保留 Python Unicode 小写规则与原 ASCII/CJK 分词范围，避免创建临时差集/并集 |
| ASR 连续重复检测 | C++17 / Python | 独立 CPU 字符内核 | C++ 使用 O(N) 后缀数组与 Lyndon 候选、精确分段和 LCP 上界剪枝；Python 参考路径 O(N log N)。不限周期，至少三次，平局取较短周期和较早起点；详见[内核说明](repeat-cpu-kernel.md) |
| ASR 复核风险统计 | Cython | 字符计数与首个热词冲突 | 保留 Python 风险权重、原因顺序及模型调用条件 |
| PCM16 采样计数、精确平方和与峰值 | Cython | 独立 CPU 字节内核 | 单遍整数计算，保留取消与 WAV 完整性检查；分块累积避免长音频溢出 |
| 候选聚类 N×N 相似度矩阵 | Rust + rayon | 归入 `_rust_speedups` | 预计算关键词并直接累计加权余弦；按行生成矩阵，拥有数据后释放 GIL，128 项起按行并行 |
| 弹幕复读率、标点强度、高情绪命中率、代表消息 | Rust | 新增 | 单遍字符串扫描和计数，Python 对象循环占比高 |
| 音频局部峰值筛选与间距去重 | Cython | 新增 | 五分钟 RMS 包络上的排序与逐点筛选是稳定数值热循环 |
| RMS 连续静音区间提取 | Cython | 新增 | 单遍阈值状态机，可避免 Python 逐帧分支 |
| 热点滚动历史稳健增幅（中位数、MAD） | Cython | 新增 | 有界独立数值缓冲区，原地排序并释放 GIL；不修改输入，列表/元组避免 NumPy 往返 |

已有的 C Aho-Corasick、字符 bigram、余弦相似度、梗词计数，以及已有的
Cython 弹幕基线、SRT 组装继续保留在同一命名空间。

## 当前模块边界

- `app.accelerators._c_speedups`：文本匹配与向量小核。
- `app.accelerators._cython_speedups`：PCM16 统计、文本新颖度、ASR 复核统计、音频包络后处理、稳健滚动基线、弹幕基线、SRT、聚类后备。
- `app.accelerators._repeat_speedups`：任意周期连续重复检测的 C++17 内核。
- `app.accelerators._rust_speedups`：rayon 聚类矩阵、并查集/簇内统计与弹幕文本特征。
- `app.accelerators.python_fallback`：与每个原生函数逐项等价的可测试参考实现。

以下链路没有改写为 Rust/Cython：RMS 包络本身已由 NumPy 向量化；FFmpeg
解码、数据库查询、LLM/ASR 调度和文件发布主要受 I/O 或外部进程限制；Brotli
解压已由现有原生依赖完成。把这些部分再包一层扩展不会缩短主要耗时，反而会
扩大跨语言状态与异常边界。

## 构建与诊断

```powershell
python setup.py build_ext --inplace --force
python tools/native/build_rust.py
```

```python
from app.accelerators import dispatcher

print(dispatcher.get_backend())
print(dispatcher.get_cython_backend())
print(dispatcher.get_rust_backend())
print(dispatcher.get_cluster_backend())
```

Windows Payload 和 Full Bundle 将当前 Python ABI 的四个 `.pyd` 视为强制发行
契约；普通源码运行仍可按函数粒度使用 Python 参考实现。

Docker 同样构建 Rust 弹幕与矩阵内核，并在真实容器验收中确认加载成功。PCM 内核只读取主机内存中的小端 PCM16 字节，返回 Python 整数统计；不接收 CUDA 张量，不修改样本、设备配置或模型加载路径。没有编译扩展时使用等价 Python 参考实现。

Rust Python 绑定固定为 PyO3 0.29.3，替换存在已公布内存/线程安全问题的旧 0.22.6；构建必须遵守 Cargo.lock。继续显式要求 GIL，不随依赖升级自动声明支持未经验证的无 GIL 解释器。

矩阵时间列使用相对整数微秒，避免 Unix 浮点秒丢失精度；四位小数遵守 Python 的偶数舍入规则。不同动态时区及 Rust UTF-8 无法表示的孤立代理码点由等价参考实现处理；不会截断、替换文本或忽略计算错误。Rust 构建按平台选择唯一动态库文件名，拒绝把 Cargo 的 `.d` 依赖清单或其他平台产物当作扩展。

C 字符 bigram 按 Python Unicode 码点处理；单汉字、空白和孤立代理码点与参考实现一致，bytes 输入必须是合法 UTF-8。

并查集内核仅接收有限数值的方阵与阈值，返回根编号、成员索引和簇内均值；主题、关联及事务仍由 Python 业务层维护。文本内核只接收字符串，保留空文本、新颖度/主题变化/实体变化评分公式；证据无需实体统计时显式返回 `None`，不会假造统计值。

## 数值语义与性能边界

滚动中位数/MAD复制到经过长度溢出检查的独立数值缓冲区，排序不修改调用方历史；有限值过滤保留，不使用 `-ffast-math` 或 `/fp:fast`。列表/元组直接进入内核，其他数值序列转换为连续 float64。Rust 矩阵预计算关键词集合、直接累计加权余弦，按行计算上三角后镜像填充；释放 GIL 时仅访问 Rust 拥有的数据。小矩阵串行，大矩阵按行并行；没有修改 Torch/CTranslate2 或设备选择。

完整调用边界基准：`python scripts/benchmark_cpu_kernels.py --output benchmark.json`。测量包括参数转换和结果返回，先逐项校验等价，再记录七轮中位数。合成 CPU 基准不代表 ASR/CUDA 或数据库端到端吞吐，也不保证未测平台及所有输入均加速。

Rust 依赖的完整许可证文本随源码、Docker 和 Windows Payload 分发；Payload 内路径为 `tools/native/rust/THIRD_PARTY_NOTICES.md`。发行验收逐字节核对该文件与源码一致。
