@echo off
setlocal EnableExtensions
chcp 65001 >nul 2>&1
cd /d "%~dp0"

rem 公网共享模式：启动题库 + 建立 Cloudflare 隧道，生成一个同事在任何网络下都能打开的网址。
rem 网址每次启动都会变，窗口关闭即失效。

if not exist "main.py" goto missing_main
if not exist "tools\cloudflared.exe" goto missing_tunnel

set "PYTHON_EXE="
if exist "%CD%\python\python.exe" set "PYTHON_EXE=%CD%\python\python.exe"
if not defined PYTHON_EXE if exist "%CD%\venv\Scripts\python.exe" set "PYTHON_EXE=%CD%\venv\Scripts\python.exe"
if not defined PYTHON_EXE goto missing_python

"%PYTHON_EXE%" -B -m scripts.share_public %*
set "SHARE_EXIT=%ERRORLEVEL%"
if "%MATHBANK_NO_PAUSE%"=="1" exit /b %SHARE_EXIT%
echo.
pause
exit /b %SHARE_EXIT%

:missing_main
echo [E_PROJECT_ROOT] 找不到 main.py，请在完整的题库项目目录里运行这个文件。
pause
exit /b 1

:missing_tunnel
echo [E_TUNNEL] 缺少穿透程序 tools\cloudflared.exe。
echo 请把 cloudflared.exe 放到项目的 tools 目录下再试。
pause
exit /b 1

:missing_python
echo [E_PYTHON] 找不到项目自带的 Python 运行环境（python\python.exe 或 venv）。
echo 请改用「启动题库系统.bat」先完成环境准备。
pause
exit /b 1
