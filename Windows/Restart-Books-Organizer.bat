@echo off
setlocal
cd /d "%~dp0.."

call "%~dp0Stop-Books-Organizer.bat"
if errorlevel 1 exit /b 1
call "%~dp0Start-Books-Organizer.bat"
