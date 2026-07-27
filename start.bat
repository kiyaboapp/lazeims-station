@echo off
REM LAZEIMS Station - one-click launcher (Windows).
REM Sets up a local .venv, installs pinned deps, runs migrations, and starts
REM the server on the LAN. Later runs need no network.
setlocal enabledelayedexpansion
cd /d "%~dp0"

set PORT=8080
if not "%STATION_PORT%"=="" set PORT=%STATION_PORT%

echo == LAZEIMS Station ==

REM 1) locate a real Python (ignore the Windows Store stub)
set PY=
for %%P in (py.exe python.exe) do (
  if "!PY!"=="" (
    where %%P >nul 2>nul && set PY=%%P
  )
)
if "!PY!"=="" (
  echo ERROR: Python 3.11+ not found. Install from python.org and re-run.
  pause
  exit /b 1
)

REM 2) create venv
if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  !PY! -m venv .venv
)

REM 3) install dependencies
if not exist ".venv\.deps_installed" (
  echo Installing dependencies...
  if exist "wheelhouse" (
    .venv\Scripts\pip install --no-index --find-links wheelhouse -e ..\lazeims-common -e . >nul
  ) else (
    .venv\Scripts\pip install -q -e ..\lazeims-common -e . >nul
  )
  echo done > .venv\.deps_installed
)

REM 4) migrate DB
.venv\Scripts\python -c "from station.config import load_config; from station.db import connect; from station.migrations import apply_migrations; c=connect(load_config().db_path); print('schema v'+str(apply_migrations(c)))"

REM 5) detect LAN IPv4
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do (
  if "!IP!"=="" set IP=%%a
)
set IP=!IP: =!
if "!IP!"=="" set IP=127.0.0.1

echo.
echo   Open on this device : http://127.0.0.1:%PORT%
echo   Open on the LAN     : http://!IP!:%PORT%
echo.

REM 6) start server
.venv\Scripts\python -m uvicorn station.main:app --host 0.0.0.0 --port %PORT%
pause
