@echo off
REM BiliLiveCut Docker 启动脚本 (Windows)
REM 从仓库根目录运行: scripts\docker-up.bat
cd /d "%~dp0.."
echo [BiliLiveCut Docker] 构建并启动...
docker compose -f packaging/docker/compose.yaml up --build -d --wait --wait-timeout 180
if errorlevel 1 exit /b 1
echo [BiliLiveCut Docker] 服务已启动: http://localhost:8000
