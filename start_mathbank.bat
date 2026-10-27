@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>&1
cd /d "%~dp0"

if not exist "main.py" goto missing_main

rem Prefer the project venv; the launcher module handles env checks itself.
if not exist "venv\Scripts\python.exe" goto missing_venv

"venv\Scripts\python.exe" -B -m scripts.windows_launcher %*
set "LAUNCHER_EXIT=%ERRORLEVEL%"
if "%LAUNCHER_EXIT%"=="0" goto launcher_done
if "%MATHBANK_NO_PAUSE%"=="1" goto launcher_done
echo.
echo Press any key to close...
pause >nul

:launcher_done
exit /b %LAUNCHER_EXIT%

:missing_main
echo [E_PROJECT_ROOT] main.py not found beside this launcher.
pause
exit /b 1

:missing_venv
echo [E_SOURCE_VENV] venv\Scripts\python.exe not found.
echo Run the setup once, or launch via the original BAT file.
pause
exit /b 1
