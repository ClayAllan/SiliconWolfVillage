@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
set "APP_DIR=%~dp0"
for %%I in ("%APP_DIR%.") do set "APP_DIR=%%~fI"
set "APP_PORT=8503"

if defined SWV_PYTHON_EXE (
    set "PYTHON_EXE=%SWV_PYTHON_EXE%"
    if not exist "!PYTHON_EXE!" goto missing_python
) else (
    where python >nul 2>nul
    if errorlevel 1 goto missing_python
    set "PYTHON_EXE=python"
)

if not exist "%APP_DIR%\app.py" goto missing_app
cd /d "%APP_DIR%"
if errorlevel 1 goto failed
"!PYTHON_EXE!" -X utf8 -c "import socket,sys; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1); s.bind(('127.0.0.1',int(sys.argv[1]))); s.close()" "%APP_PORT%" 2>nul
if errorlevel 1 goto port_busy
echo [INFO] Project: %APP_DIR%
echo [INFO] URL: http://127.0.0.1:%APP_PORT%
echo [INFO] Press Ctrl+C to stop this server.
if /i "%~1"=="--check" exit /b 0
"!PYTHON_EXE!" -X utf8 -m streamlit run "%APP_DIR%\app.py" --server.address 127.0.0.1 --server.port %APP_PORT%
if errorlevel 1 goto failed
exit /b 0

:missing_python
echo [ERROR] Python was not found.
echo [INFO] Install Python and add it to PATH, or set SWV_PYTHON_EXE to the full python.exe path.
goto failed

:missing_app
echo [ERROR] App was not found: %APP_DIR%\app.py
goto failed

:port_busy
echo [ERROR] Port %APP_PORT% is already in use. No server was started.
echo [INFO] Stop only the previous SiliconWolfVillage server, then retry.

:failed
if /i "%~1"=="--check" exit /b 1
pause
exit /b 1
