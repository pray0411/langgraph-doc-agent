@echo off
chcp 65001 >nul
title Pray Stopper
echo Stopping Pray service...

rem Find and kill the process listening on port 8000
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000.*LISTENING"') do (
    echo Killing PID=%%a
    taskkill /f /pid %%a >nul 2>nul
)

echo Service stopped.
timeout /t 2 >nul
