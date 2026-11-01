@echo off
REM ============================================================
REM agent-team launcher (double-click)
REM
REM This file is intentionally ASCII-only: cmd.exe reads .bat files
REM in the console codepage, so non-ASCII text here breaks on some
REM machines. All user-facing Chinese output comes from Python.
REM
REM Usage: double-click, or  agent-team-launcher.bat [--port 8899]
REM ============================================================
setlocal
cd /d "%~dp0.."

set "PY=python"
where python >nul 2>&1 || set "PY=py -3"
%PY% --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.10+ and tick "Add Python to PATH".
    pause
    exit /b 1
)

%PY% -m agent_team.launcher %*
if errorlevel 1 (
    echo.
    echo [ERROR] Launch failed - see the output above. Log: room\worker.log
    pause
    exit /b 1
)

REM Give the user a moment to read the address, then close this window.
REM The server itself is detached, so it keeps running.
timeout /t 3 /nobreak >nul
exit /b 0