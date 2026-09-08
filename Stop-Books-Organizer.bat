@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo The application environment is not installed.
  exit /b 0
)

".venv\Scripts\python.exe" server_control.py stop
if errorlevel 1 pause
