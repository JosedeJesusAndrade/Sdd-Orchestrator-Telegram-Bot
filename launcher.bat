@echo off
REM ================================================================
REM  OpenCode Telegram Bridge — Babysitter Launcher (launcher.bat)
REM
REM  Mantiene el bot vivo. Si el bot se cierra con exit code 42,
REM  hace git pull y lo reinicia automáticamente.
REM
REM  USO:     launcher.bat
REM  ALTERNATIVA: El viejo run_bot.bat sigue existiendo para arranque
REM  manual sin auto-reinicio.
REM ================================================================
title OpenCode Telegram Bridge Launcher
cd /d %~dp0

:start
echo [%time%] Iniciando OpenCode Bot...
call .venv\Scripts\activate.bat
python bot.py

:: Python se cerró — evaluar código de salida
if %errorlevel% equ 42 (
    echo.
    echo [%time%] ============================================
    echo [%time%]  Comando /update recibido desde Telegram
    echo [%time%]  Descargando últimos cambios de GitHub...
    echo [%time%] ============================================

    git checkout main
    git pull origin main

    echo.
    echo [%time%] Reiniciando el bot en 3 segundos...
    timeout /t 3 >nul
    goto start
)

echo.
echo [%time%] El bot se apagó normalmente o hubo un error.
echo.
pause
