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
rem NOTE: this copies the real key next to the exe for LOCAL testing only.
rem installer/build_installer.py explicitly excludes .env from packages.
if exist ".env" (
    copy /y ".env" "dist\Pray\.env" >nul
    echo   Copied .env next to Pray.exe  ^(local run only - never packaged^)
) else (
    echo   [WARN] No .env in project root - create dist\Pray\.env manually
    echo          before running the exe.
)
echo.
echo ============================================================
echo   Build the distributable package?  (zip + PraySetup.exe)
echo   Requires the exe above to be built first.
echo ============================================================
set /p ANSWER=Build installer now? [Y/n] 
if /i "%ANSWER%"=="n" (
    echo   Skipped. Run: python installer\build_installer.py
    echo.
    pause
    exit /b 0
)
python installer\build_installer.py
if errorlevel 1 (
    echo.
    echo [ERROR] Packaging failed. See output above.
    pause
    exit /b 1
)
echo.
echo   dist\Pray-VERSION-win64.zip     ^(send this to others^)
echo   dist\PraySetup.exe              ^(send this to others, simplest^)
echo.
pause
