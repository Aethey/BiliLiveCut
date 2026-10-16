# 入门

本栏目集中介绍 BiliLiveCut 怎么部署、怎么配置，以及第一次怎么用。完整工作流是：直播录制或录播导入 → 语音转写 → 高光识别 → 人工审核 → 剪辑出片 → 可选上传。

## 快速开始

1. [选择安装方式](getting-started.md)，按自己的环境阅读对应部署说明。
2. 完成[后台认证与 Bilibili 登录](login.md)，按需调整[配置](configuration.md)。
3. 按[第一次完成工作流](quick-start.md)，添加直播间或导入视频，验证转写、审核与出片。

## 开始部署

- [Windows Portable](../../packaging/portable/USER_GUIDE_ZH.md)：Full/Lite 下载、模型准备与启动。
- [Linux 无头服务器](linux-server.md)：uv 安装、CPU 转写依赖、启动与浏览器访问。
- [Docker 部署](../../packaging/docker/README.md)：容器启动、管理员初始化与数据持久化。
- [源码部署](source.md)：在 Windows、macOS 或 Linux 上通过 uv 或 venv + pip 安装并运行 Python 源码。

## 配置与登录

管理员身份与 Bilibili 平台身份分别管理，扫码不要求服务器具备桌面。配置参考解释环境默认值、Web 覆盖、作用范围与生效时间；模型依赖和模型文件需要按所选引擎另行准备。

Alpha 升级使用独立运行目录和当前版本数据库，保留旧环境；模型复用仍需通过完整性检查。不要修改数据库版本号绕过校验。

## 继续使用

| 你要完成的任务 | 阅读入口 |
| --- | --- |
| 日常录制、导入、转写、审核与上传 | [功能指南](../features/README.md) |
| 修改前端、开发插件或维护发行 | [开发与扩展](../development/README.md) |
| 理解高光检测、原生加速与重复检测算法 | [技术参考](../reference/README.md) |
| 查询当前版本、历史变化和第三方许可 | [版本与许可](../releases/README.md) |

只有 Web 与录制依赖时，可以先录制或导入；实际转写仍需准备引擎和模型。远程转写、LLM 分析与自动上传分别按配置启用，安装成功不能代替完整链路的实际验收。
