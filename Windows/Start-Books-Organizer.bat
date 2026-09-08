@echo off
setlocal
cd /d "%~dp0.."

if exist ".venv\Scripts\python.exe" goto check_dependencies

where py >nul 2>&1
if errorlevel 1 goto create_with_python
py -3 -m venv .venv
goto check_created

:create_with_python
python -m venv .venv

:check_created
if errorlevel 1 goto failed

:check_dependencies
".venv\Scripts\python.exe" -c "import fastapi, uvicorn, pypdfium2, PIL, google.genai" >nul 2>&1
if not errorlevel 1 goto start_server
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed

:start_server
".venv\Scripts\python.exe" server_control.py start
if errorlevel 1 goto failed
exit /b 0

:failed
echo.
echo The application could not start. Review data\server.log if it exists.
pause
exit /b 1
