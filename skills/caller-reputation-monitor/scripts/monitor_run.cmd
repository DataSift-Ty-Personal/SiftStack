@echo off
REM monitor_run.cmd - Task Scheduler wrapper for the Caller ID Reputation Monitor.
REM Runs a daily LIVE scan. Pass extra flags through, e.g.:
REM   monitor_run.cmd --always-notify
REM Uses whatever `python` is on PATH; set PYEXE below to pin a specific interpreter.
setlocal
set "SUBDIR=%~dp0"
if "%SUBDIR:~-1%"=="\" set "SUBDIR=%SUBDIR:~0,-1%"
set "PYEXE=python"

cd /d "%SUBDIR%"
if not exist "%SUBDIR%\logs" mkdir "%SUBDIR%\logs"

echo. >> "%SUBDIR%\logs\monitor_wrapper.log"
echo ==== run %DATE% %TIME% args=%* ==== >> "%SUBDIR%\logs\monitor_wrapper.log"
"%PYEXE%" "%SUBDIR%\monitor.py" --commit %* >> "%SUBDIR%\logs\monitor_wrapper.log" 2>&1
set "RC=%ERRORLEVEL%"
echo ==== exit %RC% ==== >> "%SUBDIR%\logs\monitor_wrapper.log"
endlocal & exit /b %RC%
