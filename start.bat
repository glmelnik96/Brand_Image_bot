@echo off
REM =====================================================================
REM  Phygital-bot - start.
REM  Double-click runs the bot in this console window with logs.
REM  Ctrl+C stops it. After exit, window stays open (pause) so you can
REM  read the traceback if the bot crashed.
REM
REM  ASCII-only on purpose: cmd.exe parses .bat files in the active OEM
REM  codepage (cp866 on RU Windows). Non-ASCII chars in this file break
REM  the parser. chcp below switches only the *output* encoding.
REM =====================================================================

chcp 65001 > nul
title Phygital-bot

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv not found in %CD%
    echo.
    echo Set it up first:
    echo     py -3.11 -m venv .venv
    echo     .venv\Scripts\pip install -r requirements.txt
    echo     .venv\Scripts\playwright install chromium
    echo.
    pause
    exit /b 1
)

if not exist ".env" (
    echo [ERROR] .env not found in %CD%
    echo Copy .env.example to .env and fill TELEGRAM_BOT_TOKEN etc.
    pause
    exit /b 1
)

REM Make sure no other instance of this bot is already polling Telegram,
REM otherwise we get telegram.error.Conflict immediately.
powershell -NoProfile -Command "$existing = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*bot.main*' -and $_.ProcessId -ne $PID }; if ($existing) { Write-Host '[ERROR] Another bot.main process is already running:'; $existing | Select-Object ProcessId, CommandLine | Format-List; Write-Host 'Run stop.bat first.'; exit 1 } else { exit 0 }"
if errorlevel 1 (
    pause
    exit /b 1
)

echo --------------------------------------------------------------
echo  Phygital-bot
echo  cwd: %CD%
echo  Press Ctrl+C to stop.
echo --------------------------------------------------------------
echo.

".venv\Scripts\python.exe" -m bot.main

set EXIT_CODE=%ERRORLEVEL%
echo.
echo --------------------------------------------------------------
echo  Bot stopped (exit code: %EXIT_CODE%)
echo --------------------------------------------------------------
pause
