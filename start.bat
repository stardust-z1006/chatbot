@echo off
rem deepChat v1 launcher.
rem Keep this file ASCII-only: cmd reads batch files in the console codepage,
rem and chcp 65001 for the Python child would mangle any Chinese text here.
rem All Chinese UI lives in run.py / the panel instead.
title deepChat v1
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Python venv not found: .venv\Scripts\python.exe
    echo         Create it first:
    echo             python -m venv .venv
    echo             .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo         See README.md, section "Install".
    pause
    exit /b 1
)

rem UTF-8 console so Python's Chinese output stays readable.
chcp 65001 >nul
".venv\Scripts\python.exe" run.py %*
set "RC=%ERRORLEVEL%"
echo.
echo Exit code %RC%
if not "%RC%"=="0" (
    echo Press any key to keep this window.
    pause >nul
) else (
    timeout /t 2 >nul
)
