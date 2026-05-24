@echo off
REM Telegram Bridge - Standalone execution with isolated venv
cd /d %~dp0
call .venv\Scripts\activate.bat
python bot.py
pause
