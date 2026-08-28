@echo off
chcp 65001 >nul
title Pray Launcher
echo ========================================
echo    Pray - AI Agent Launcher
echo ========================================
echo.

rem Switch to this script's directory
cd /d "%~dp0"

rem Check Python
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.10+
    pause
    exit /b 1
)

rem Check dependencies (install on first run)
python -c "import langgraph" >nul 2>nul
if errorlevel 1 (
    echo [INFO] First run: installing dependencies...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Dependency install failed. Check network and retry.
        pause
        exit /b 1
    )
)

rem Check if port 8000 is already in use
netstat -ano | findstr ":8000.*LISTENING" >nul 2>nul
if not errorlevel 1 (
    echo [INFO] Port 8000 is already in use - service may already be running.
    echo        Opening browser: http://127.0.0.1:8000
    start http://127.0.0.1:8000
    pause
    exit /b 0
)

echo [START] Launching Pray...
echo [URL]   http://127.0.0.1:8000
echo [STOP]  Close this window or run stop.bat
echo.
start http://127.0.0.1:8000
python -X utf8 main.py web --port 8000

pause
