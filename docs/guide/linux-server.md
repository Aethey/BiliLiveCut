# Linux 无头服务器

Web 控制台、Bilibili 扫码登录、录制、CPU 转写与剪辑可通过无桌面的 Linux 服务器运行。浏览器在自己的电脑上打开服务器提供的页面；服务器不需要图形桌面或弹出的二维码窗口。

以下以 Ubuntu/Debian 的全新源码目录为例。容器运行另见 [Docker 部署](../../packaging/docker/README.md)，完整参数见[配置参考](configuration.md)。

## 系统依赖

准备 Python 3.12、[uv](https://docs.astral.sh/uv/getting-started/installation/)。安装编译工具、FFmpeg 和字幕字体：

```bash
sudo apt-get update
sudo apt-get install -y build-essential python3.12-dev ffmpeg fonts-noto-cjk
ffmpeg -hide_banner -version
ffmpeg -hide_banner -filters
```

Ubuntu 以外的发行版应安装对应 Python 版本的开发头文件，或使用 uv 管理的 Python。`Python.h: No such file or directory` 的处理见[原生扩展安装排错](../development/environment.md#原生扩展构建与安装排错)。FFmpeg 需要 `subtitles` 与 `drawtext` 滤镜，字幕与片头渲染会用到它们。

## 安装与构建

在全新 checkout 的项目根目录执行：

```bash
uv sync --locked --no-dev --extra web --extra asr-all --extra llm
cp -n .env.example .env
```

这是包含全部 ASR 后端的安装方式，依赖体积较大；只录制和管理时可以省略 `--extra asr-all --extra llm`。传统 pip 的对应命令为 `pip install -e ".[web,asr-all,llm]"`，虚拟环境准备见[开发说明](../development/environment.md#venv-pip)。

编辑 `.env`：设置非空 `ADMIN_PASSWORD`，为实际启用的引擎选择 CPU 设备，按需设置模型路径和服务商配置。管理员用户名为 `admin`。Bilibili Cookie 通过网页扫码单独配置，不使用管理员密码。

同步完成后使用 `uv run --no-sync` 运行当前环境，避免后续命令因省略 extras 而移除已安装的可选依赖。首次转写所需模型按所选引擎下载，安装成功并不代表模型已经准备好。

```bash
uv run --no-sync blc init
uv run --no-sync blc serve --host 127.0.0.1 --port 8000
```

默认数据保存在当前运行目录下的 `storage/`。不要在验收新版本时复用旧版本的数据库、会话库和 Cookie；数据库版本不兼容时，保留原环境并使用新目录初始化。

## 从自己的电脑访问

保持服务监听服务器的 `127.0.0.1`，在自己电脑的终端建立 SSH 隧道：

```bash
ssh -N -L 8000:127.0.0.1:8000 your-user@your-server
```

浏览器访问 `http://127.0.0.1:8000`。如果本机 8000 已占用，将命令第一个端口改为 8001，并访问 `http://127.0.0.1:8001`。

需要通过服务器地址直接访问时，先设置管理员密码，再绑定相应监听地址。对外提供服务可配置 HTTPS 反向代理；服务端的权限、Cookie 与同源 API 约束仍然适用。

## 完整验收顺序

| 步骤 | 要确认的结果 |
| --- | --- |
| 管理员登录 | 能查看直播间与设置，刷新后仍可操作 |
| Bilibili 扫码 | 用手机确认后显示账号状态和 UID |
| 短时间录制 | 生成可播放的视频与对应场次记录 |
| CPU 转写 | 实际执行所选引擎，产生有时间戳的文本 |
| 高光分析 | 产生候选片段，能进入人工审核 |
| 剪辑渲染 | 生成可播放的成品，字幕与封面符合配置 |
| 重启 | 同一版本重启后业务数据仍在，重新登录管理后台后可继续操作 |

没有直播源时可用一段已获授权的短视频完成[导入验收](../features/recording-import.md)。LLM 分析需要配置所选服务；某个引擎能够转写不能代替完整流程的实际验收。

## 常见问题

- **页面缺少样式或脚本**：确认当前安装包含 `app/web/templates/` 和 `app/web/static/`，再通过浏览器开发者工具检查静态文件请求是否成功。
- **不知道管理员密码**：查看本次运行目录所使用的 `.env` 中的 `ADMIN_PASSWORD`；不是 Bilibili 密码。
- **数据库应用版本不匹配**：保留旧目录，在独立目录初始化；不要仅修改数据库版本号绕过校验。
- **连续收到 401、403 或 429**：先确认管理员登录状态和密码，等待限流窗口恢复；不要自动重复提交错误凭据。Bilibili 二维码排错见[登录恢复说明](login.md)。
- **修改页面没有变化**：修改 HTML、CSS 或 JavaScript 后重新加载页面；静态资源仍被缓存时强制刷新。Python 服务的自动重启与页面刷新说明见 [Web 页面开发](../development/frontend.md#本地修改与刷新)。
