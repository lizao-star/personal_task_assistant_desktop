@echo off
cd /d "%~dp0\client_windows"
call .venv\Scripts\activate.bat
python run.py
pause