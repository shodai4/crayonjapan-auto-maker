@echo off
rem ===========================================================================
rem  CrayonJapan Auto-Maker - one-time Windows local setup
rem
rem  Double-click this file once. It creates the virtual environment, installs
rem  the Python packages, makes .env, creates the 01_RAW_VIDEO / 02_READY_TO_POST
rem  / 03_ARCHIVE folders and then checks the PC (python main.py --doctor).
rem
rem  The messages are in English on purpose: a Japanese .bat needs a code page
rem  switch and still shows mojibake on some PCs. The Japanese walk-through is
rem  docs\SETUP_WINDOWS_LOCAL.md.
rem ===========================================================================
setlocal EnableExtensions
cd /d "%~dp0"

echo === CrayonJapan Auto-Maker: Windows local setup ===
echo.

rem --- 1. find Python ---------------------------------------------------------
set "PY="
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
  where python >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo [FAIL] Python was not found on this PC.
  echo        Install Python 3.10 or newer from https://www.python.org/downloads/
  echo        Tick "Add python.exe to PATH" in the installer, then run this again.
  goto :fail
)
echo [1/5] Python found: %PY%

rem --- 2. virtual environment -------------------------------------------------
if exist "%~dp0.venv\Scripts\python.exe" (
  echo [1/5] Reusing the existing .venv
) else (
  echo [1/5] Creating .venv ...
  %PY% -m venv "%~dp0.venv"
  if errorlevel 1 (
    echo [FAIL] Could not create .venv
    goto :fail
  )
)
set "VPY=%~dp0.venv\Scripts\python.exe"

rem --- 3. packages ------------------------------------------------------------
echo [2/5] Updating pip ...
"%VPY%" -m pip install --upgrade pip
if errorlevel 1 goto :fail

echo [3/5] Installing the requirements ^(a few minutes the first time^) ...
"%VPY%" -m pip install -r requirements.txt
if errorlevel 1 goto :fail

rem --- 4. .env ----------------------------------------------------------------
if exist "%~dp0.env" (
  echo [4/5] .env already exists - left as it is
) else (
  copy /y "%~dp0.env.example" "%~dp0.env" >nul
  echo [4/5] Created .env from .env.example
  echo        GEMINI_API_KEY is optional: without it the script is written from
  echo        templates in config.yaml and the Short is still made and posted.
)

rem --- 5. folders + check -----------------------------------------------------
echo [5/5] Creating the folder layout and checking this PC ...
"%VPY%" "%~dp0main.py" --local-init
echo.
"%VPY%" "%~dp0main.py" --doctor
echo.

echo Next steps:
echo   1^) Google Cloud Console: create an OAuth client of type "Desktop app" with the
echo      YouTube Data API v3 and the YouTube Analytics API enabled, download the JSON
echo      and save it in this folder as  client_secret.json
echo   2^) Sign in once:            run_local.bat --auth
echo   3^) Put a GO_ folder or one recording in 01_RAW_VIDEO, then run:  run_local.bat
echo   4^) Make it automatic:      scripts\register_local_task.bat 22:00
echo.
echo   The folder walk-through is docs\SETUP_WINDOWS_LOCAL.md
echo.
pause
exit /b 0

:fail
echo.
echo Setup stopped. Fix the line above, then run setup_local.bat again.
echo.
pause
exit /b 1
