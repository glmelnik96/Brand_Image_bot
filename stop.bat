@echo off
REM =====================================================================
REM  Cloud.ru Image Bot - stop.
REM  Finds all python.exe processes whose CommandLine mentions bot.main
REM  and kills them. ASCII-only on purpose (see start.bat for why).
REM =====================================================================

chcp 65001 > nul
title Cloud.ru Image Bot stop

powershell -NoProfile -Command "$procs = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*bot.main*' }; if (-not $procs) { Write-Host '[OK] No bot.main process is running.'; exit 0 } else { Write-Host ('[INFO] Found ' + $procs.Count + ' bot.main process(es):'); $procs | Select-Object ProcessId, CommandLine | Format-List; foreach ($p in $procs) { Write-Host ('[KILL] PID ' + $p.ProcessId); Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }; Start-Sleep -Milliseconds 500; $left = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*bot.main*' }; if ($left) { Write-Host '[ERROR] Some processes did not die:'; $left | Select-Object ProcessId, CommandLine | Format-List; exit 1 } else { Write-Host '[OK] All bot.main processes stopped.'; exit 0 } }"

set EXIT_CODE=%ERRORLEVEL%
echo.
pause
exit /b %EXIT_CODE%
