@echo off
chcp 65001 >nul
title Update Pray
cd /d "%~dp0"

echo ============================================================
echo   Update Pray
echo   [1] pull latest code   [2] install dependencies   [3] rebuild exe
echo ============================================================
echo.

rem ---------- locate git (PATH first, then bundled runtime) ----------
set "GIT="
where git >nul 2>nul && set "GIT=git"
if not defined GIT if exist "%USERPROFILE%\.flareos\runtime\git\2.51.2\cmd\git.exe" set "GIT=%USERPROFILE%\.flareos\runtime\git\2.51.2\cmd\git.exe"

if defined GIT (
    echo [1/3] Pulling latest code...
    "%GIT%" pull --ff-only
    if errorlevel 1 (
        echo.
        echo [WARN] Pull failed - no network / proxy down / local changes.
        echo        Continuing with the code currently on disk.
    )
) else (
    echo [1/3] Git not found - skipping code update.
    echo        Install Git, or copy new code over this folder manually.
)

rem ---------- dependencies ----------
echo.
echo [2/3] Checking dependencies...
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.10+ and retry.
    pause
    exit /b 1
)
python -m pip install --disable-pip-version-check -q -r requirements.txt
echo       runtime dependencies OK
python -c "import webview" >nul 2>nul
if errorlevel 1 (
    echo       installing desktop dependency...
    python -m pip install --disable-pip-version-check -q -r requirements-desktop.txt
)

rem ---------- rebuild exe (optional) ----------
echo.
echo [3/3] Rebuild Pray.exe?
echo       Not needed if you launch Pray with start-pray.bat - new code
echo       takes effect the next time you start it.
echo       Needed only if you double-click dist\Pray\Pray.exe.
echo.
if /i "%~1"=="--no-build" goto skip_build
if /i "%~1"=="-n" goto skip_build

choice /c YN /n /m "      Rebuild now? [Y/N]: "
if errorlevel 2 goto skip_build

echo.
echo       Building... takes 1-2 minutes.
python -m pip install --disable-pip-version-check -q pyinstaller
python -m PyInstaller Pray.spec --noconfirm --clean
if errorlevel 1 (
    echo.
    echo [ERROR] Build failed - see output above.
    pause
    exit /b 1
)
echo.
echo ============================================================
echo   Updated.  New executable:  dist\Pray\Pray.exe
echo   Your desktop shortcut already points there - just run it.
echo ============================================================
echo.
pause
exit /b 0

:skip_build
echo.
echo ============================================================
echo   Updated.  Start Pray with start-pray.bat (always latest code).
echo ============================================================
echo.
pause
