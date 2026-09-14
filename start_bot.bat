@echo off
chcp 65001 >nul
title Telegram AI-bot (DeepSeek)
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

:loop
echo.
echo === Запускаю бота... ===
"%PY%" main.py
echo.
echo Бот остановился. Перезапуск через 10 секунд (закрой окно, чтобы выйти).
timeout /t 10 >nul
goto loop
