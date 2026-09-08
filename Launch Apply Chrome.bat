@echo off
setlocal
set PROFILE=%~dp0chrome-profile
set CHROME=
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if exist "%LocalAppData%\Google\Chrome\Application\chrome.exe" set "CHROME=%LocalAppData%\Google\Chrome\Application\chrome.exe"
if "%CHROME%"=="" (
  echo Could not find chrome.exe. Edit this file and set CHROME to your Chrome path.
  pause
  exit /b 1
)
echo Launching your Apply Chrome (debug port 9222)...
echo Profile: %PROFILE%
start "" "%CHROME%" --remote-debugging-port=9222 --user-data-dir="%PROFILE%"
