@echo off
REM ================================================================
REM  Start Penates: one click. Safe to run again (it restarts cleanly).
REM   1. make sure Ollama (the local model) is running
REM   2. stop any old Penates server on port 8765
REM   3. start a fresh server (minimized window)
REM   4. confirm the new server answers, then open the dashboard
REM ================================================================
cd /d "%~dp0"
title Start Penates

echo [1/4] Local model (Ollama)...
curl -s -m 2 http://127.0.0.1:11434/api/tags >nul 2>&1
if errorlevel 1 (
  where ollama >nul 2>&1
  if errorlevel 1 (
    echo       Ollama is not installed or not on PATH. Get it from https://ollama.com then run this again.
    echo       Penates will still start, but answers that need the model will fail.
  ) else (
    echo       starting Ollama...
    start "Ollama" /min ollama serve
    for /l %%i in (1,1,15) do (
      curl -s -m 1 http://127.0.0.1:11434/api/tags >nul 2>&1 && goto :ollama_up
      timeout /t 1 /nobreak >nul
    )
    echo       Ollama did not answer yet. Check the "Ollama" window.
  )
) else (
  echo       already running.
)
:ollama_up

echo [2/4] Stopping any old Penates server...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8765" ^| findstr LISTENING') do (
  echo       stopping PID %%a
  taskkill /F /PID %%a >nul 2>&1
)
timeout /t 1 /nobreak >nul

echo [3/4] Starting the Penates server...
start "Penates Server" /min cmd /k python serve.py

echo [4/4] Waiting for it to answer...
for /l %%i in (1,1,20) do (
  curl -s -m 1 http://127.0.0.1:8765/version >nul 2>&1 && goto :server_up
  timeout /t 1 /nobreak >nul
)
echo       The server did not start. Open the "Penates Server" window on the taskbar for the error.
pause
exit /b 1

:server_up
curl -s http://127.0.0.1:8765/version
echo.
echo       Penates is running. Opening the dashboard...
start "" http://127.0.0.1:8765/
timeout /t 3 /nobreak >nul
exit /b 0
