@echo off
rem ===========================================================================
rem  CrayonJapan Auto-Maker - one local run (the posting slot)
rem
rem  Scans 01_RAW_VIDEO, renders ONE waiting artwork, moves the package into
rem  02_READY_TO_POST, uploads it to YouTube (only when the 48 h interval has
rem  passed) and moves the source to 03_ARCHIVE. Google Drive is not used.
rem
rem  Usage:
rem    run_local.bat                 one publishing slot (this is what the
rem                                  scheduled task runs)
rem    run_local.bat --no-upload     render only, no YouTube upload
rem    run_local.bat --dry-run       show what would happen, change nothing
rem    run_local.bat --auth          sign in to YouTube once in the browser
rem    run_local.bat --local-watch   keep watching the folder every 60 s
rem
rem  Every argument is passed straight to main.py.
rem ===========================================================================
setlocal EnableExtensions
cd /d "%~dp0"

set "VPY=%~dp0.venv\Scripts\python.exe"
if not exist "%VPY%" (
  echo [FAIL] .venv is missing - run setup_local.bat once first.
  exit /b 1
)

rem The log folder is the one local_hub uses (<base>\logs).
set "LOGDIR="
for /f "delims=" %%L in ('"%VPY%" -c "import local_hub;print(local_hub.load_local_folders().logs)" 2^>nul') do set "LOGDIR=%%L"
if not defined LOGDIR set "LOGDIR=%~dp0logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%" >nul 2>nul
set "LOG=%LOGDIR%\run_local.log"

echo CrayonJapan local run - log: %LOG%
echo.
"%VPY%" "%~dp0main.py" %* >> "%LOG%" 2>&1
set "CODE=%ERRORLEVEL%"

rem Show the tail of the log so the window is never silent.
powershell -NoProfile -Command "Get-Content -LiteralPath '%LOG%' -Tail 40" 2>nul
if errorlevel 1 type "%LOG%"

echo.
if "%CODE%"=="0" (
  echo Done. Exit code 0.
) else (
  echo Something failed - exit code %CODE%. The full log is:
  echo   %LOG%
)
exit /b %CODE%
