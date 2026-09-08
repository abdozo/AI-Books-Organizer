@echo off
setlocal
cd /d "%~dp0"

call Stop-Books-Organizer.bat
if errorlevel 1 exit /b 1
call Start-Books-Organizer.bat
