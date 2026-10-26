@echo off
setlocal
chcp 65001 >nul
set "APP_DIR=F:\Github\SiliconWolfVillage"
set "PYTHON_EXE=D:\anaconda3\python.exe"
set "APP_PORT=8503"
if not exist "%PYTHON_EXE%" goto missing_python
if not exist "%APP_DIR%\app.py" goto missing_app
cd /d "%APP_DIR%"
if errorlevel 1 goto failed
"%PYTHON_EXE%" -X utf8 -c "import socket,sys; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1); s.bind(('127.0.0.1',int(sys.argv[1]))); s.close()" "%APP_PORT%" 2>nul
if errorlevel 1 goto port_busy
echo [INFO] Project: %APP_DIR%
echo [INFO] URL: http://127.0.0.1:%APP_PORT%
echo [INFO] Press Ctrl+C to stop this server.
if /i "%~1"=="--check" exit /b 0
"%PYTHON_EXE%" -X utf8 -m streamlit run "%APP_DIR%\app.py" --server.address 127.0.0.1 --server.port %APP_PORT%
if errorlevel 1 goto failed
exit /b 0
:missing_python
echo [ERROR] Python was not found: %PYTHON_EXE%
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
