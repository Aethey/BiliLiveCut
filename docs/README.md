---
home: true
title: BiliLiveCut
permalink: /
config:
  - type: hero
    effect: tint-plate
    effectConfig:
      light: '240,248,252'
      dark: '22,30,38'
    hero:
      name: BiliLiveCut
      tagline: 从录播到粗剪切片
      text: 录制、转写、识别高光与人工审核，将直播内容和录播转换为高光切片。
      actions:
        - theme: brand
          text: 快速开始
          link: /guide/quick-start.html
        - theme: alt
          text: 安装部署
          link: /guide/installation.html
        - theme: alt
          text: GitHub
          link: https://github.com/StarGazerQQD/BiliLiveCut
  - type: features
    features:
      - title: 直播录制与录播导入
        icon: 🎬
        details: 自动检测开播并录制，也可导入已有视频与弹幕，在同一工作台继续分析。
        link: /features/imports.html
      - title: 多引擎语音转写
        icon: 🎙️
        details: 使用 FunASR、Whisper 等本地引擎处理语音，支持 CPU 运行与远程转写。
        link: /features/asr.html
      - title: 高光识别与人工审核
        icon: ✨
        details: 结合弹幕、音频与语义信号生成候选，通过时间线和人工审核确定精彩片段。
        link: /reference/hotspot-detector.html
      - title: 剪辑出片与扩展
        icon: ✂️
        details: 生成字幕、封面和切片，按需上传；通过插件扩展直播源与分析能力。
        link: /development/plugins.html
  - type: custom
---

## 选择适合你的运行方式

- **Windows 桌面**：使用 [Portable](../packaging/portable/USER_GUIDE_ZH.md)，Full 自带 Python 与 FFmpeg。
- **Linux 无头服务器**：按[服务器部署指南](guide/linux-server.md)安装，在自己的浏览器中完成扫码登录和日常管理。
- **Docker**：按[容器部署说明](../packaging/docker/README.md)运行，持久化保存配置、模型与录制数据。
- **Python 源码**：按[源码部署指南](guide/source.md)安装，在 Windows、macOS 或 Linux 上通过 Python 启动。

## 源码快速启动

在全新的项目目录中，准备 Python 3.12、FFmpeg 和编译工具。将 `.env.example` 复制为 `.env`，已存在时保留原文件，按需编辑后执行：

```bash
uv sync --locked --no-dev --extra web
uv run --no-sync blc init
uv run --no-sync blc serve
```

打开 `http://127.0.0.1:8000`，添加直播间或导入已有视频。完整安装步骤及 venv + pip 方式见[源码部署](guide/source.md)。

::: tip 准备转写与分析
上面的命令先安装 Web 管理和录制所需依赖。需要本地转写与 LLM 分析时，按[完整安装说明](guide/source.md#按需安装转写与分析依赖)补充可选依赖、模型和服务配置。管理员登录与 Bilibili 扫码登录是两个独立步骤。
:::

## 插件扩展

按需安装独立插件，扩展高光评分、直播来源和音频分析能力：

- **个性化高光评分**：[Highlight](https://github.com/StarGazerQQD/BiliLiveCut_Highlight) 结合转写、弹幕与音频特征评估候选片段，支持使用人工审核反馈训练模型，让评分更贴近自己的选片偏好。
- **抖音直播接入**：[DouyinCompatibility](https://github.com/StarGazerQQD/BiliLiveCut_DouyinCompatibility) 提供直播间解析、开播检测与取流，沿用主程序的录制、转写、分析和切片流程。目前不采集抖音弹幕。
- **语音与音乐检测**：[inaSpeech](https://github.com/StarGazerQQD/BiliLiveCut_inaSpeech) 识别语音、音乐、噪声和静音的时间区间及占比，为录播分析补充音频分类信息。目前仅记录分析结果，不改变高光评分；推理服务需单独部署在 Linux、Docker 或 WSL2 环境中。

安装步骤、依赖与兼容要求见各插件仓库。Highlight 与 inaSpeech 使用同一高光评分插件入口，需择一启用。

## 从哪里开始

- 第一次使用：阅读[安装方式导览](guide/getting-started.md)和[使用指南](features/README.md)。
- 分析已有视频：查看[录播导入与恢复](features/recording-import.md)。
- 调整常用项与高级参数：查看[配置参考](guide/configuration.md)。
- 修改 Web 页面：查看 [Web 页面开发与验证](development/frontend.md)。
- 开发新平台或分析能力：查看[插件开发](../plugin/README.md)与[直播源契约](development/live-source-plugins.md)。
- 了解版本变化：查看[更新记录](../CHANGELOG.md)，完整目录见[文档导览](guide/README.md)。

::: warning Alpha 版本升级
升级时保留旧运行目录，在独立目录初始化当前版本。数据库的应用版本必须与程序一致，即使 Schema 编号相同，也不能直接复用旧数据库。详见[升级说明](guide/getting-started.md#升级前保留旧环境)。
:::
