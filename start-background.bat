@echo off
chcp 65001 >nul
title Pray Background Launcher
rem Launch Pray in background (no console window)

cd /d "%~dp0"

rem Check if port 8000 is already in use
netstat -ano | findstr ":8000.*LISTENING" >nul 2>nul
if not errorlevel 1 (
    echo Service already running. Opening browser: http://127.0.0.1:8000
    start http://127.0.0.1:8000
    timeout /t 2 >nul
    exit /b 0
)

rem Launch in background (minimized window)
start "" /min python -X utf8 main.py web --port 8000
timeout /t 3 >nul

start http://127.0.0.1:8000
echo Pray is running in background: http://127.0.0.1:8000
echo To stop the service, run stop.bat
timeout /t 3 >nul
