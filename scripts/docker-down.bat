@echo off
REM BiliLiveCut Docker 停止脚本 (Windows)
cd /d "%~dp0.."
echo [BiliLiveCut Docker] 停止服务...
docker compose -f packaging/docker/compose.yaml down
if errorlevel 1 exit /b 1
echo [BiliLiveCut Docker] 已停止
