@echo off
rem Paperatomy workbench launcher: double-click to start, browser opens automatically.
cd /d "%~dp0"
set "PY=venv\Scripts\python.exe"
if exist "%PY%" goto run
set "PY=C:\Users\29397\.workbuddy\binaries\python\envs\paperatomy\Scripts\python.exe"
if exist "%PY%" goto run
set "PY=python"
:run
"%PY%" -m paperatomy.cli serve --open
pause
