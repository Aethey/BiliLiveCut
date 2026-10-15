# 选择安装方式

BiliLiveCut 的完整流程是：添加直播间 → 录制或导入视频 → 转写与识别高光 → 人工审核 → 生成切片 → 可选上传。先选部署方式，再准备实际需要的模型和服务。

## 安装入口

| 运行环境 | 建议入口 | 需要准备 |
| --- | --- | --- |
| Windows 桌面 | [Portable 使用说明](../../packaging/portable/USER_GUIDE_ZH.md) | Full 自带 Python 和 FFmpeg，模型另行准备 |
| Linux 无头服务器 | [Linux 部署指南](linux-server.md) | Python、FFmpeg；原生扩展构建需要编译工具和 Python 开发头文件 |
| Docker | [容器部署](../../packaging/docker/README.md) | Docker，持久化数据目录和模型空间 |
| Windows、macOS 或 Linux 源码运行 | [源码部署](source.md) | Python、FFmpeg；默认原生扩展安装需要编译工具和 Python 开发头文件 |
| 修改代码或参与开发 | [开发环境](../development/environment.md) | uv 或 venv + pip、FFmpeg、编译工具；JavaScript 检查和文档站另需 Node.js |

源码安装同时支持 uv 和传统 venv + pip。Web 控制台直接使用随程序提供的 HTML 模板、CSS 和 JavaScript，无需安装 Node.js 或单独构建页面。JavaScript 开发检查和文档站使用独立的 Node.js 工具。

## 第一次运行

1. 启动 Web 服务，打开终端给出的地址。
2. 根据部署方式完成管理员登录或首次初始化。管理员账号与 Bilibili 账号是两套独立的登录。
3. 在“Bilibili 登录”页面扫码。无头服务器通过浏览器显示二维码，无需服务器桌面。
4. 添加一个直播间，进行短时间录制；已有视频可直接使用[录播导入](../features/recording-import.md)。
5. 准备所选 ASR 模型和分析服务，再测试转写、高光识别、审核与渲染。

只有录制和 Web 管理需求时，可以先不安装 ASR/LLM 可选依赖。完整本地转写需要对应依赖、模型文件及足够的内存与磁盘空间，首次模型下载可能耗时。

## 接下来读什么

- 日常操作、CLI、转写与排错：[使用指南](../features/README.md)。
- 参数含义、常用/高级设置与生效时间：[配置参考](configuration.md)。
- 修改与检查 Web 页面：[Web 页面开发与验证](../development/frontend.md)。
- 二次开发与平台扩展：[插件开发](../../plugin/README.md)、[直播源插件](../development/live-source-plugins.md)。

## 升级前保留旧环境

当前 Alpha 版本要求数据库中的应用版本与程序一致；Schema 编号相同也不代表可以直接复用。升级时保留旧目录和数据库，在独立目录初始化当前版本，再重新配置。模型内容可按现有完整性校验规则复用，具体边界见[项目说明](../../README.md#使用与升级注意事项)。

## Python 依赖源

默认可使用官方 PyPI。需要镜像时，现有源码指南提供以下 PowerShell 进程级配置；这些变量用于 pip 安装，不写入应用 `.env`：

```powershell
$env:PIP_INDEX_URL='https://mirrors.aliyun.com/pypi/simple/'
$env:PIP_EXTRA_INDEX_URL='https://pypi.tuna.tsinghua.edu.cn/simple/'
```

依赖仍以当前版本的 `pyproject.toml` 与 uv 锁文件为准。原生扩展的编译器和头文件准备见[开发环境](../development/environment.md#原生扩展构建与安装排错)。
