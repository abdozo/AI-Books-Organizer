@echo off
setlocal
cd /d "%~dp0.."
title Update AI Books Organizer

if not exist ".git" goto not_a_git_clone

where git >nul 2>&1
if errorlevel 1 goto git_not_found

echo Stopping AI Books Organizer...
call "%~dp0Stop-Books-Organizer.bat"
if errorlevel 1 goto stop_failed

echo Switching to the main branch...
git checkout main
if errorlevel 1 goto update_failed

echo Downloading the latest version...
git pull --ff-only origin main
if errorlevel 1 goto update_failed

echo Starting AI Books Organizer...
call "%~dp0Start-Books-Organizer.bat"
if errorlevel 1 goto start_failed

echo.
echo Update completed successfully.
timeout /t 3 >nul
exit /b 0

:not_a_git_clone
echo.
echo Update failed: this folder is not a Git clone.
echo Download or clone the application from GitHub before using this updater.
goto failed

:git_not_found
echo.
echo Update failed: Git is not installed or is not available in PATH.
echo Install Git for Windows, then run Update.bat again.
goto failed

:stop_failed
echo.
echo Update cancelled because the server could not be stopped.
goto failed

:update_failed
echo.
echo Update failed. Your local files were not deleted.
echo Resolve the Git error shown above, then run Update.bat again.
goto failed

:start_failed
echo.
echo The update finished, but the application could not start.
echo Review data\server.log if it exists.

:failed
echo.
pause
exit /b 1
