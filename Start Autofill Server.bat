@echo off
cd /d "%~dp0"
echo Starting the apply autofill server...
python serve.py
pause
