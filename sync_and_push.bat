@echo off
cd /d "%~dp0"
python sync_and_push.py
if errorlevel 1 pause
