@echo off
rem Paperatomy workbench launcher: double-click to start, browser opens automatically.
cd /d "%~dp0"
rem 先清掉占用 8770 的残留旧进程，避免「旧服务接新页面」的错位
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8770 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"
set "PY=venv\Scripts\python.exe"
if exist "%PY%" goto run
set "PY=C:\Users\29397\.workbuddy\binaries\python\envs\paperatomy\Scripts\python.exe"
if exist "%PY%" goto run
set "PY=python"
:run
"%PY%" -m paperatomy.cli serve --open
pause
