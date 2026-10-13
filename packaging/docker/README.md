# BiliLiveCut Docker 部署

本目录提供 BiliLiveCut `v0.1.18.8-Alpha` 的 CPU 镜像。构建上下文为仓库根目录；容器以 UID/GID 1000 的 `appuser` 运行单实例 Web 服务，内含 FFmpeg、中文字体、C/Cython 扩展及配套 CPU PyTorch/TorchAudio。业务操作见[使用指南](../../docs/usage.md)。

## 首次启动

需要 Docker Engine 和支持 `up --wait` 的 Docker Compose v2。在仓库根目录执行：

```bash
test -f .env || cp .env.example .env
mkdir -p storage
# 编辑 .env，设置非空 ADMIN_PASSWORD；用户名为 admin。
# Linux 上确保 storage 可由 UID/GID 1000 写入；不要用 chmod 777。
docker compose -f packaging/docker/compose.yaml up --build -d --wait --wait-timeout 180
```

访问 `http://localhost:8000`，使用 `admin` 和配置的密码登录。镜像入口在启动前检查密码和目录写权限；缺少密码或目录不可写时明确失败。宿主绑定目录的权限不会被镜像内 chown 自动修复。可通过调整宿主目录权限解决，或明确执行以下一次性初始化（会改变 storage 内文件的所有者）：

```bash
docker compose -f packaging/docker/compose.yaml run --rm --user root \
  --entrypoint chown app -R 1000:1000 /data
```

Web 服务在容器内监听 `0.0.0.0:8000`，Compose 将宿主 8000 端口映射到它。只需要本机或 SSH 隧道访问时，可将端口映射改为 `127.0.0.1:8000:8000`。

## 启动、查看和停止

```bash
bash scripts/docker-up.sh
docker compose -f packaging/docker/compose.yaml logs -f app
docker compose -f packaging/docker/compose.yaml ps
bash scripts/docker-down.sh
```

Windows 对应 `scripts\docker-up.bat` / `scripts\docker-down.bat`。脚本可以从任意工作目录启动，命令失败会返回非零，启动脚本等待 Web 健康检查通过才提示成功。HTTP 健康检查直接访问本机服务，绕过外部代理，不读取认证凭据；200 和认证挑战 401 均表示 Web 已能响应，不代表模型推理成功。

Compose 使用 `init: true` 回收子进程，停止宽限为 60 秒，为 FFmpeg 收尾和 Worker 关闭留出时间。重启策略为 `unless-stopped`，手动停止后不会自动重新启动。不要增加 Web workers 或复用同一数据库启动多个容器：当前录制监控和任务调度属于单实例应用。

## 数据与缓存

宿主 `storage/` 挂到容器 `/data`，覆盖数据库、录制、成片、日志、插件和模型缓存：

| 内容 | 容器路径 |
|------|---------|
| SQLite | `/data/blc.db` |
| 录制、成片与日志 | `/data/raw`、`/data/clips`、`/data/logs` |
| 插件 | `/data/plugins` |
| Launcher 运行配置 | `/data/config` |
| Hugging Face 缓存 | `/data/cache/huggingface` |
| ModelScope 缓存 | `/data/cache/modelscope` |
| 可选浏览器缓存 | `/data/cache/playwright` |

旧版本 Compose 的 `hf-cache` 卷挂到 `/root`，与 appuser 的缓存位置不匹配；新配置改为 `/data/cache`，不会自动删除或迁移旧卷。重建或删除容器保留宿主 storage；不要将旧 Schema 数据库覆盖到当前目录。当前版本只接受 Schema v6。

镜像默认 CPU 推理，模型权重仍在首次使用时下载，因此需要足够磁盘、内存和可访问的模型源。镜像构建和 CI 只验证真实 SDK 导入与服务运行，不下载转写模型、不测量真实转写吞吐。GPU 部署需另外准备匹配的 CUDA 镜像和设备透传；修改设备配置为 cuda 不能使这个 CPU 镜像支持 GPU。

## 登录限制

Web 与录制不需要桌面。当前“自动登录”功能在运行应用的机器打开有界面 Chromium，容器没有远程桌面或二维码转发入口；请在设置页手动填写 Bilibili Cookie。此镜像不预装浏览器桌面组件，浏览器包和 Xvfb 也不能单独解决远程交互问题。

## 构建和依赖更新

构建阶段通过固定 uv 版本，以 `uv.lock` 安装运行 extras，复制完整 setuptools 数据文件和 C/Cython 构建源，并进行非 editable 安装。运行阶段只复制环境，不携带编译器、uv 或仓库源码。传统 pip 源码安装流程不变。

项目 extras 暂未声明 PyTorch，CPU 镜像另外使用 `requirements-cpu.lock` 的官方 CPU wheel、完整配套依赖和 SHA-256 哈希。共享依赖版本来自 uv.lock；最终安装使用 `--inexact` 保留 CPU 运行时，再执行依赖一致性和真实导入校验。

更新项目锁后，在仓库根目录重新生成 CPU 依赖锁并验证镜像：

```bash
mkdir -p .local
uv export --locked --no-dev --extra asr-all --extra llm --extra web \
  --no-emit-project --no-hashes --output-file .local/docker-runtime-constraints.txt
uv pip compile packaging/docker/requirements-cpu.in \
  --constraint .local/docker-runtime-constraints.txt \
  --python-version 3.12 --python-platform x86_64-manylinux_2_28 \
  --index-url https://pypi.org/simple --extra-index-url https://download.pytorch.org/whl/cpu \
  --index-strategy unsafe-best-match --generate-hashes --no-annotate \
  --output-file packaging/docker/requirements-cpu.lock
docker build -t bili-live-cut:docker-test -f packaging/docker/Dockerfile .
python scripts/smoke_docker.py
```

当前验收目标为 Linux x86_64 / Python 3.12，其他架构应重新生成对应锁并完成实际构建验收。Docker 工作流执行实际镜像构建与隔离容器检查，包括密码/权限失败、认证、CPU ASR 导入、原生扩展、FFmpeg 滤镜、中文字体、数据持久化以及停止后重建。验收使用临时卷，不读取仓库 .env，也不连接真实直播或下载模型。
