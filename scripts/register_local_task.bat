@echo off
rem ===========================================================================
rem  CrayonJapan Auto-Maker - register the daily posting slot
rem
rem  Creates (or replaces) a Windows Task Scheduler entry that runs
rem  run_local.bat every day at the time given, 22:00 by default (the same
rem  22:00 JST slot the GitHub Actions cron used). The run itself decides with
rem  YouTube whether 48 h have passed, so a daily trigger is safe: on the days
rem  inside the interval it exits without rendering or posting anything.
rem
rem  Usage:
rem    register_local_task.bat            every day at 22:00
rem    register_local_task.bat 07:30      every day at 07:30
rem    register_local_task.bat 22:00 /delete   remove the entry
rem
rem  No administrator rights are needed: the task runs as you, only while you
rem  are logged on. Keep the PC on (or wake it) at that time.
rem ===========================================================================
setlocal EnableExtensions
cd /d "%~dp0"
set "REPO=%~dp0"
rem strip the trailing backslash
if "%REPO:~-1%"=="\" set "REPO=%REPO:~0,-1%"

set "AT=22:00"
if not "%~1"=="" set "AT=%~1"

rem The task name comes from config.yaml (local.task_name) when python is ready.
set "TASK=CrayonJapan Auto-Maker"
if exist "%~dp0.venv\Scripts\python.exe" (
  for /f "delims=" %%T in ('"%~dp0.venv\Scripts\python.exe" -c "import yaml;c=yaml.safe_load(open('config.yaml',encoding='utf-8')) or {};print(((c.get('local') or {}).get('task_name') or 'CrayonJapan Auto-Maker'))" 2^>nul') do set "TASK=%%T"
)

if /i "%~2"=="/delete" goto :delete

if not exist "%~dp0run_local.bat" (
  echo [FAIL] run_local.bat is missing next to this script.
  exit /b 1
)

set "TR=\"%REPO%\run_local.bat\""

echo Registering "%TASK%" - every day at %AT% ...
schtasks /Create /TN "%TASK%" /SC DAILY /ST %AT% /TR "%TR%" /RL LIMITED /F
if errorlevel 1 (
  echo.
  echo [FAIL] Windows refused to create the task.
  echo        Check the time format ^(HH:MM, 24 h^) and that this folder still exists:
  echo        %REPO%
  exit /b 1
)

echo.
echo Registered:
schtasks /Query /TN "%TASK%" /V /FO LIST | findstr /i "TaskName Status Next Run Time Task To Run"
echo.
echo Test it now:        schtasks /Run /TN "%TASK%"
echo Remove it:          "%~f0" %AT% /delete
echo Change the time:    "%~f0" 07:30
echo.
echo The task runs only while you are logged on, and only while the PC is on.
echo A run inside the 48 h interval renders nothing and posts nothing.
exit /b 0

:delete
schtasks /Delete /TN "%TASK%" /F
if errorlevel 1 (
  echo [FAIL] No task called "%TASK%" was found.
  exit /b 1
)
echo Removed "%TASK%".
exit /b 0
