@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo  Pray installer
echo  --------------
echo  Installing to: %%LOCALAPPDATA%%\Programs\Pray
echo  (no admin rights required)
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
if errorlevel 1 (
    echo.
    echo  Installation failed. See the message above.
    pause
    exit /b 1
)
pause
