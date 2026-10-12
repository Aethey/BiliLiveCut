# BiliLiveCut — AI 直播实时切片系统

[![CI](https://github.com/StarGazerQQD/BiliLiveCut/actions/workflows/ci.yml/badge.svg)](https://github.com/StarGazerQQD/BiliLiveCut/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/StarGazerQQD/BiliLiveCut?include_prereleases&sort=semver)](https://github.com/StarGazerQQD/BiliLiveCut/releases)
[![License](https://img.shields.io/github/license/StarGazerQQD/BiliLiveCut)](LICENSE)

**当前版本：V0.1.18.8 Alpha** (`0.1.18.8-alpha`) · [更新记录](CHANGELOG.md)

面向 Bilibili 直播的自动切片工具：**录制 → 转写 → 识别高光 → 人工审核 → 生成切片与文案 → 可选上传**。通过 Web 控制台管理直播间、录制场次、转写、候选和成品。

## 核心功能

- **开播自动录制与分析**：按直播间开启自动化或预约录制，运行期间同步直播间标题。
- **本地多引擎语音处理**：Fun-ASR-Nano、Paraformer、Whisper 负责转写与回退，SenseVoice 提供辅助特征；也可显式选择 OpenAI 兼容远程转写；可选大模型整理正文和生成文案。
- **已有录播分析**：独立页面导入视频与可选 XML、JSON、SRT、ASS 弹幕，预处理后由所选 ASR 引擎转写，再按配置提交 LLM 分析，支持继续上传和失败重试。
- **弹幕烧录**：独立于 ASR 字幕，支持滚动、顶部与下方样式及字号、颜色、不透明度和密度设置，按场次时间轴用于主片和派生版本。
- **场次高光时间线**：融合弹幕、音频、语音与趋势信号识别事件，支持跨连续分段的动态剪辑边界。
- **审核与出片**：支持人工修订、多人审核、字幕、封面和后台渲染，并保存任务状态。
- **统一设置与扩展**：Web 设置中心管理业务配置，支持房间级开关、模型服务商和插件。
- **直播源插件**：通过正式契约接入其他平台，共用监控、录制和分析流水线；平台取流由独立插件提供，弹幕能力可选。

## 快速开始

### Windows Portable

1. 从 [Releases](https://github.com/StarGazerQQD/BiliLiveCut/releases) 下载 Portable **Full** 或 **Lite**。Full 自带 Python、依赖和 FFmpeg；Lite 需要准备 Python 3.11/3.12、FFmpeg，并在首次安装时下载依赖。
2. 按 [Portable 小白使用说明](packaging/portable/USER_GUIDE_ZH.md) 完成启动和模型准备。两种发行包均不含 ASR 模型，可在线下载，或使用单独的 Engine Pack。
3. 在 Web 控制台添加已获授权的直播间，开始录制；需要无人值守时开启“开播后自动录制并分析”。

Engine Pack 的本地构建与导入方式见 [Portable 构建说明](packaging/portable/README.md)。模型包约 5.5 GiB，GitHub Release 不提供该文件。

### 源码运行

准备 FFmpeg，在已下载的项目根目录选择一种安装方式。C/Cython 扩展需要编译器与 Python 开发头文件，准备方法见[开发说明](docs/development.md#原生扩展构建与安装排错)。

使用 [uv](https://docs.astral.sh/uv/getting-started/installation/)（默认 Python 3.12），以下 PowerShell 命令安装运行依赖和全部 ASR、LLM 后端：

```powershell
uv sync --locked --no-dev --extra web --extra asr-all --extra llm
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
uv run --locked --no-dev --extra web --extra asr-all --extra llm blc init
uv run --locked --no-dev --extra web --extra asr-all --extra llm blc serve
```

仅录制和使用 Web 后台时可以省略 `--extra asr-all --extra llm`，后续 `uv run` 也使用同样的 extras。开发环境直接执行 `uv sync --locked`，详见[开发说明](docs/development.md)。

使用传统 venv + pip（Python 3.11/3.12），安装方式继续保留：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[web,asr-all,llm]"
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
python -m app.cli init
python -m app.cli serve
```

默认访问 `http://127.0.0.1:8000`。直播间、录制与大部分业务配置都可在 Web 页面管理；安装镜像、CLI 录制和排错见[使用指南](docs/usage.md)。Docker 部署见 [Docker 说明](packaging/docker/README.md)。

## 使用与升级注意事项

- **不迁移旧版本数据库、配置别名、接口或运行时。** 升级请保留旧目录，在全新目录初始化 Schema 6 数据库，按当前 `.env.example` 和 Web 设置重新配置。模型内容可独立复用：支持 schema 5 Engine Pack 及 schema 7 安装清单，发行版本或源码提交不同仅提示来源差异，仍须通过当前模型仓库、revision、格式和文件完整性检查；不支持的 schema 不会自动升级。不要直接复制旧数据库、配置或运行时覆盖新目录。
- 自动开播检测需要服务持续运行；程序退出或系统休眠期间不会检测。
- 本地模型处理需要相应计算资源；录直播、采集弹幕、远程转写及调用远程大模型需要联网。大模型与自动上传均可按需启用。
- Web 业务设置持久化保存，具体生效时间可在设置中心查看。数据库、存储路径和登录身份等启动配置仍通过环境配置管理；远程部署必须设置 `ADMIN_PASSWORD`。
- 仅录制你拥有授权的内容，遵守平台条款。默认手动上传模式只生成待上传材料；启用自动投稿前需配置上传工具和对应开关。

## 文档

| 需要了解 | 入口 |
| --- | --- |
| 下载、首次启动与第一次录制 | [Portable 小白使用说明](packaging/portable/USER_GUIDE_ZH.md) |
| CLI、转写、审核、上传与排错 | [使用指南](docs/usage.md) |
| 已有视频、弹幕导入与智能分析 | [录播导入](docs/recording-import.md) |
| Web 设置、环境变量与生效边界 | [配置参考](docs/configuration.md) |
| 高光事件、评分与剪辑边界 | [热点检测器](docs/hotspot-detector.md) |
| 开发、测试与发行维护 | [开发与维护](docs/development.md) |
| 插件接口与示例 | [插件开发](plugin/README.md) |
| 直播源契约、平台身份及房间登记 | [直播源插件](docs/live-source-plugins.md) |
| 全部指南与版本历史 | [文档索引](docs/README.md) · [CHANGELOG](CHANGELOG.md) |

## 鸣谢

感谢每一位提供反馈、报告问题及支持本项目的朋友。

- GitHub：[shuangyiliu211126com](https://github.com/shuangyiliu211126com)（[#61](https://github.com/StarGazerQQD/BiliLiveCut/issues/61)、[#62](https://github.com/StarGazerQQD/BiliLiveCut/issues/62)）
- GitHub：[xiu1zi3](https://github.com/xiu1zi3)（[#60](https://github.com/StarGazerQQD/BiliLiveCut/issues/60)）

- Bilibili：[大口径喷子](https://space.bilibili.com/13886027)
- Bilibili：[口米_K-Mi](https://space.bilibili.com/493488643)
- Bilibili：[_铃子](https://space.bilibili.com/95763745)
- Bilibili：[爱莉希雅office](https://space.bilibili.com/11241685)
- Bilibili：[七圣小伞](https://space.bilibili.com/330443150)
- Bilibili：[溯园](https://space.bilibili.com/22335588)
- Bilibili：[桜墨Moku](https://space.bilibili.com/6432607)
- Bilibili：[TDD-怡汁汽水糖](https://space.bilibili.com/1137908003)
- Bilibili：[伯丶牙](https://space.bilibili.com/21959755)

## 许可证

BiliLiveCut 项目代码采用 [MIT License](LICENSE)，Copyright (c) 2026 StarGazerQQD。第三方模型及组件适用各自的许可证；项目的 MIT License 不改变任何第三方条款。详见[第三方模型声明](packaging/portable/licenses/THIRD_PARTY_NOTICES.md)。
