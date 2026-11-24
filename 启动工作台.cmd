@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
if exist ".venv\Scripts\python.exe" goto install
if exist "venv\Scripts\python.exe" goto existing
where py >nul 2>&1
if errorlevel 1 goto python
py -3 -m venv .venv
if not errorlevel 1 goto install
:python
python -m venv .venv
if errorlevel 1 goto failed
:install
set "PY=.venv\Scripts\python.exe"
goto check
:existing
set "PY=venv\Scripts\python.exe"
:check
"%PY%" -c "import fitz, yaml" >nul 2>&1
if not errorlevel 1 goto run
"%PY%" -m pip install .
if errorlevel 1 goto failed
:run
"%PY%" -m paperatomy.cli serve --open
if errorlevel 1 goto failed
exit /b 0
:failed
echo.
echo Paperatomy could not start. Check the message above.
echo Install Python 3.10 or newer and check your network connection.
echo See README.md for manual installation and troubleshooting.
pause
exit /b 1
