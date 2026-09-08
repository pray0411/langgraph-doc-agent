@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo  ============================================
echo   Pray MCP demo client (stdio)
echo   Type your question to chat via 'ask' tool.
echo   /new = new session   /mem = view memory   /exit = quit
echo  ============================================
echo.
"C:\Users\MSN\.flareos\runtime\python\3.12.0\python.exe" -X utf8 mcp_demo.py
echo.
pause
