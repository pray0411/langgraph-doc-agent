@echo off
chcp 65001 >nul
title Pray Launcher
cd /d "%~dp0"

echo ============================================================
echo   Pray  -  ReAct Agent Launcher
echo ============================================================
echo.

rem ---------- 1. locate python ----------
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY ( where py >nul 2>nul && set "PY=py" )
if not defined PY (
    echo [ERROR] Python not found.
    echo         Install Python 3.10+ from https://www.python.org/downloads/
    echo         During install, check "Add python.exe to PATH".
    echo.
    pause
    exit /b 1
)
echo [OK] Python found.

rem ---------- 2. prepare .env ----------
if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
        echo [WARN] No .env found - created one from .env.example.
        echo        Fill in your API key ^(e.g. DEEPSEEK_API_KEY^) and run again.
        echo.
        notepad ".env"
        pause
        exit /b 0
    )
)

rem ---------- 3. install dependencies on first run ----------
%PY% -c "import langgraph" >nul 2>nul
if errorlevel 1 (
    echo [INFO] First run: installing dependencies, this may take several minutes...
    echo.
    %PY% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [ERROR] Dependency installation failed. Check network / proxy, then retry.
        pause
        exit /b 1
    )
    echo.
    echo [OK] Dependencies installed.
)

rem ---------- 4. launch desktop window if possible, else web ----------
%PY% -c "import webview" >nul 2>nul
if errorlevel 1 (
    echo [INFO] pywebview not installed - starting web version.
    echo        Browser will open at http://127.0.0.1:8000  ^(close this window to stop^)
    echo.
    start "" http://127.0.0.1:8000
    %PY% -X utf8 main.py web
) else (
    echo [INFO] Starting desktop window...
    echo.
    %PY% -X utf8 desktop.py
)

echo.
echo Pray stopped.
pause
