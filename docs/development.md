# 开发与维护

项目入口见[README](../README.md)，业务链路见[使用指南](usage.md)和[热点检测器](hotspot-detector.md)。具体 Portable 构建步骤见[构建说明](../packaging/portable/README.md)。

## 开发环境

使用 Python 3.11/3.12，准备 FFmpeg 和 Node.js。在隔离虚拟环境中安装开发依赖：

```powershell
pip install -e ".[dev,web,asr-all,llm]"
```

源码依赖要求 `sqlmodel>=0.0.22,<0.0.45`，Portable 完整锁继续使用 `0.0.39`。当前 Schema 与 API 使用既有无时区 UTC 时间约定；[SQLModel 0.0.45 起改变默认日期时间行为](https://sqlmodel.tiangolo.com/advanced/datetime/#upgrade-existing-applications)，直接升级会影响入库、查询、时间比较与响应格式。依赖上限用于保持现有数据契约，不触发数据库迁移。

Portable 依赖锁固定 `hydra-core==1.3.7` 和 `urllib3==2.8.0`，对应 [Hydra 安全修复](https://github.com/hydra-ecosystem/hydra/releases/tag/v1.3.7)与 [urllib3 安全修复](https://github.com/urllib3/urllib3/releases/tag/2.8.0)。两者沿用现有依赖范围，纯 Python wheel 适用于两个 Windows ABI；更新时核对实际下载文件的 SHA-256，运行双 ABI 依赖解析及审计。Hydra 会拒绝危险配置目标，urllib3 更严格地隔离 HTTPS 代理和目标站点 TLS 配置；自定义第三方配置不能依赖旧的不安全行为。

Windows 的 `FFMPEG_PATH`、`FFPROBE_PATH` 应指向真实二进制文件，避免指向 Chocolatey 的 `bin` 包装程序。包装进程可能在停止时留下仍持有管道的 FFmpeg 子进程。Windows CI 共用 `scripts/download_release_ffmpeg.py` 的 Release 下载入口，沿用 BtbN/Gyan 来源、每源最多三次尝试及 ZIP 完整性校验，绕开 Chocolatey 服务超时后返回成功但未安装文件的问题。通过 `--github-env` 指定 Actions 环境文件时，脚本先执行两个二进制的版本探测并确认 `subtitles`、`drawtext` 滤镜，全部成功后才写入真实绝对路径，并把已验证目录加入后续步骤的 PATH；失败保留具体原因并返回非零状态。Portable Full 已使用随包二进制的绝对路径。

Python 镜像配置见[使用指南](usage.md#python-依赖源)。普通源码运行缺少原生扩展时可按函数回退到 Python 参考实现；完整验证和 Portable 发行需要对应 ABI 的 C、Cython、Rust 三个扩展。Windows C/Cython 构建需要可用的 MSVC 工具链，Rust 需要 Rust 工具链。

```powershell
python setup.py build_ext --inplace
python tools/native/build_rust.py
```

模块职责、函数清单和诊断命令见[原生加速模块](native-acceleration.md)。开发与验收使用独立的数据库、存储和配置，不复用正在运行实例的 `.env`、Cookie、数据库或媒体。

## 验证

先执行改动对应的目标测试，再运行完整测试和质量检查。项目的 `pytest` 默认收集 `tests/` 与 `packaging/portable/tests/`：

CI 使用 `pip install -e ".[dev,web]"` 安装依赖。`dev` 复用 `asr-whisper` extra，包含无语音分类与独立 ASR 子进程回归需要的真实音频解码、CPU Silero VAD 及其 SDK 依赖；这些测试不需要 GPU，也不下载转写模型。验证依赖变更时应新建隔离虚拟环境，避免开发机已安装的 ASR SDK 掩盖声明遗漏；CI 保持 `ASR_NO_MODEL_DOWNLOAD=1`。

`asr-whisper` 显式要求 `av>=11,<19`。当前 faster-whisper 1.2.1 仍向 `av.open()` 传入 `metadata_errors`，而 PyAV 19 已移除此参数（见[上游兼容性问题](https://github.com/SYSTRAN/faster-whisper/issues/1492)）；无上限安装会在音频解码前抛出 `TypeError`。Portable 的两个 ABI 锁继续使用已验证的 `av==18.0.0`。将来放开上限前，应先验证真实解码、CPU VAD 及独立子进程回归。

```powershell
python -m pytest
python scripts/run_ruff.py check
python scripts/run_ruff.py format
python scripts/check_version_consistency.py
python scripts/check_changelog_archive.py
node scripts/check_frontend_interactions.mjs
node scripts/check_configuration_interactions.mjs
```

GitHub Actions 的 macOS 全量测试仅在 `main` 推送时运行。覆盖率测试步骤上限为 90 分钟，整个 job 上限为 120 分钟，为依赖安装和报告上传保留余量。超时配置更新仅对使用新提交的运行生效，重跑旧提交仍使用其原有时限。

macOS 字幕和片头测试需要带 `subtitles`、`drawtext` 滤镜的 FFmpeg。CI 安装 Homebrew 的 [`ffmpeg-full`](https://formulae.brew.sh/formula/ffmpeg-full)，通过 `brew --prefix --installed ffmpeg-full` 解析路径，并显式设置 `FFMPEG_PATH`、`FFPROBE_PATH` 和后续步骤的 `PATH`。该配方为 keg-only，仅安装它不会替换默认路径中的普通 `ffmpeg`；普通版缺少这些滤镜时会导致真实烧录测试失败。安装步骤检查两个可执行文件和所需滤镜，缺失时在完整测试前直接报错，不跳过字幕或片头测试。

测试重试和退避时，应先隔离被测模块持有的时钟与随机数引用，再替换等待或随机函数；直接 patch 模块引用中的标准库属性会影响整个进程，可能把后台线程的等待误计为重试。数据库退避测试会主动启动另一个线程，验证它的等待不会进入重试记录。

完整门禁需要可用的原生扩展、真实 Payload 等前置产物，缺少前置条件时应先完成构建；不将跳过项目计为通过。以下入口按仓库配置执行依赖审计、测试和相关构建检查：

```powershell
# 提交前 CI 门禁
python scripts/ci_gate.py
# 发布前门禁：拒绝测试跳过、无效审计结果及不完整或不可复现的产物
python scripts/release_gate.py
```

真实高光插件联调需要单独提供插件仓库，命令见[使用指南](usage.md#可插拔高光评分)，不属于宿主默认测试集。

## 版本与源码冻结

- 应用版本以 [`app/__init__.py`](../app/__init__.py) 为事实来源。升级时同步包元数据、原生模块、Portable 配置、文档与产物命名，再执行版本一致性检查。模型 revision 和 schema 根据真实契约变化维护，不随补丁版本机械递增。
- **Source Commit** 标识内嵌业务源码快照，**Builder Commit** 标识本次运行构建工具的提交。先提交需要进入 Payload 的业务源码与版本，再更新 Portable 的源码冻结配置；构建工具和文档可以在后续提交维护。
- Payload 构建检查其包含范围内的已跟踪差异和新增未跟踪文件，拒绝工作区业务源码与冻结提交不一致。范围由[源码快照模块](../packaging/portable/src/blc_portable/payload/source_snapshot.py)定义，不通过忽略检查或手工复制工作区文件绕过。
- 快照通过 Git 提取，构建只校验其自身声明的版本，不在构建期改写旧源码版本。默认重新提取、构建两轮并比较 ZIP SHA-256；原生模块在构建 checkout 中编译，随后复制匹配 ABI 的产物到打包目录。

## 发行契约

- Payload 保存文件清单与 SHA-256，Runtime 在安全解压和验证后通过 staging、锁和原子切换激活；用户配置、数据库、模型和媒体不随业务源码覆盖。具体目录布局见[Portable 构建说明](../packaging/portable/README.md)。
- 数据库、Runtime、Payload 和模型清单遵循当前明确支持的格式；不能从历史重构记录推断迁移能力。模型安装清单按当前 schema、模型身份与文件内容检查，允许发行来源不同但内容相同的模型复用；在线下载续传记录及构建器仍校验其发行身份。旧 schema 不迁移。
- 真实 Engine Pack 与 fixture 必须明确区分。Fixture 的摘要来自实际构建文件；它只用于验证，不能作为正式模型包分发。主模型、子模型和随附组件使用统一模型目录，保留所需许可证和来源证据。
- 发布清单记录最终文件名、版本、大小、SHA-256 和 CRC32；跨制品检查源码、构建器、Payload 与版本身份。重新构建后的摘要必须重新计算，并检查源码路径、敏感配置、缓存和临时文件未进入产物。

## 可选本地 ASR 集成检查

完成 Engine Pack 安装后，可以显式运行真实模型检查（默认 CPU/int8），不会自动下载模型、调用 LLM 或访问现有业务数据库：

```powershell
python scripts/check_local_asr.py --models-dir "D:/测试安装/models" --audio "D:/测试音频/speech.wav" --engine whisper --expect speech --contains "已知的语音文字" --output .local/local-asr.json
```

`--engine` 可选 `whisper`、`paraformer`、`funasr_nano`；`--device cuda` 用于具备匹配驱动的 GPU 环境；合法静音使用 `--expect no_speech` 并去掉 `--contains`。命令先逐文件重算已安装模型的 SHA-256，再执行生产隔离流水线，核对结果分类、非空语音文本和时间戳，并确认进程回收。输入、依赖、模型或推理异常直接失败，报告不包含转写正文。运行前按当前依赖安装运行环境并提供 FFmpeg；该检查独立于 CI 的确定性回归，CPU 通过不能代替现场 GPU 故障复现。

## 文档约定

- README 提供项目简介、核心功能、快速开始、重要限制和文档入口；详细用法进入对应指南，避免每个版本继续追加历史小节。
- 用户可见变更记入当前系列的 `CHANGELOG.md`；旧系列使用既有 `docs/changelog/` 归档，不重复维护多份版本历史。
- 长期有效的配置说明、架构决策和发行约束留在正式文档，随实现同步维护。一次性任务清单、排查过程和运行日志可留在 PR、Issue 或本地 `.local/` 中，不作为当前使用指南。
- `.local/` 从 Git 跟踪、源码分发包和 Docker 构建上下文中排除。整理历史记录时先提炼仍有效的内容，核对引用，再保留本地原件；不要通过重写 Git 历史删除已经提交的记录。
