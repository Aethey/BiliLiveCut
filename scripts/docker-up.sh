#!/bin/bash
# BiliLiveCut Docker 启动脚本 (Linux/macOS)
# 从仓库根目录运行: bash scripts/docker-up.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
echo "[BiliLiveCut Docker] 构建并启动..."
docker compose -f packaging/docker/compose.yaml up --build -d --wait --wait-timeout 180
echo "[BiliLiveCut Docker] 服务已启动: http://localhost:8000"
