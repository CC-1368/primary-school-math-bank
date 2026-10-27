@echo off
setlocal EnableExtensions
chcp 65001 >nul 2>&1
cd /d "%~dp0"

rem 局域网共享模式：监听本机所有网卡，同事在同一 Wi-Fi / 同一交换机下可访问。
rem 如需恢复仅本机使用，请改用「启动题库系统.bat」。
set "MATHBANK_HOST=0.0.0.0"

call "%~dp0启动题库系统.bat" %*
exit /b %ERRORLEVEL%
