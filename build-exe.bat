@echo off
chcp 65001 >nul
title Build Pray.exe
cd /d "%~dp0"

echo ============================================================
echo   Build Pray.exe  (PyInstaller)
echo ============================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.10+ first.
    pause
    exit /b 1
)

python -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
    echo [INFO] Installing PyInstaller...
    python -m pip install pyinstaller
    if errorlevel 1 ( echo [ERROR] Failed to install PyInstaller. & pause & exit /b 1 )
)

python -c "import langgraph" >nul 2>nul
if errorlevel 1 (
    echo [INFO] Installing runtime dependencies...
    python -m pip install -r requirements.txt
    if errorlevel 1 ( echo [ERROR] Failed to install dependencies. & pause & exit /b 1 )
)

echo [INFO] Building... this takes a few minutes.
echo.
python -m PyInstaller Pray.spec --noconfirm --clean
if errorlevel 1 (
    echo.
    echo [ERROR] Build failed. See output above.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   Done.  Executable:  dist\Pray\Pray.exe
echo   Double-click it to run (no Python required on target PC).
echo ============================================================

rem ---------- carry over .env so the exe finds the API key ----------
if exist ".env" (
    copy /y ".env" "dist\Pray\.env" >nul
    echo   Copied .env next to Pray.exe
) else (
    echo   [WARN] No .env in project root - create dist\Pray\.env manually
    echo          before running the exe.
)
echo.
pause
