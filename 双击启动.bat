@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

echo ==========================================
echo    group-brain  v0.1  one-click start
echo ==========================================
echo.

rem ---- 1. find a real python (the Store stub does not count) ----
set "PYCMD="
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYCMD=py -3"
if not defined PYCMD (
  python -c "import sys" >nul 2>nul
  if not errorlevel 1 set "PYCMD=python"
)
rem PATH may still be stale right after installing, so probe known locations too
if not defined PYCMD call :trypy "%LOCALAPPDATA%\Programs\Python\Launcher\py.exe"
if not defined PYCMD call :trypy "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PYCMD call :trypy "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PYCMD call :trypy "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PYCMD call :trypy "C:\Python313\python.exe"
if not defined PYCMD call :trypy "C:\Python312\python.exe"
if not defined PYCMD call :trypy "C:\Python311\python.exe"
if not defined PYCMD call :trypy "%ProgramFiles%\Python313\python.exe"
if not defined PYCMD call :trypy "%ProgramFiles%\Python312\python.exe"
if not defined PYCMD call :trypy "%ProgramFiles%\Python311\python.exe"

if not defined PYCMD (
  echo [X] Python not found on this machine.
  echo.
  echo     How to fix:
  echo       1. double-click the installer "python-3.12.x-amd64.exe"
  echo       2. CHECK "Add python.exe to PATH" at the bottom of the first screen
  echo       3. close this window and double-click this file again
  echo.
  pause
  exit /b 1
)

echo [0/4] python: !PYCMD!

rem ---- 2. virtual environment ----
if not exist ".venv\Scripts\python.exe" (
  echo [1/4] creating virtual environment ...
  !PYCMD! -m venv .venv
  if errorlevel 1 (
    echo [X] venv creation failed.
    pause
    exit /b 1
  )
) else (
  echo [1/4] virtual environment already exists, skip
)

call ".venv\Scripts\activate.bat"

rem ---- 3. dependencies ----
echo [2/4] checking dependencies ...
python -m pip install -r requirements.txt -q --disable-pip-version-check
if errorlevel 1 (
  echo [X] pip install failed.
  echo     try: python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
  pause
  exit /b 1
)

rem ---- 4. build index ----
echo [3/4] building index ...
python scripts\init_db.py
if errorlevel 1 goto fail
python scripts\import_memes.py
if errorlevel 1 goto fail
python scripts\import_messages.py
if errorlevel 1 goto fail

echo.
echo [4/4] starting server on http://127.0.0.1:8000
echo       browser opens in a second.
echo       KEEP THIS WINDOW OPEN. close it to stop the server.
echo.
start "" "http://127.0.0.1:8000"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --log-level warning
echo.
echo server stopped.
pause
exit /b 0

:trypy
if defined PYCMD goto :eof
if not exist "%~1" goto :eof
"%~1" -c "import sys" >nul 2>nul
if errorlevel 1 goto :eof
set "PYCMD="%~1""
exit /b 0

:fail
echo.
echo [X] setup failed, read the message above.
pause
exit /b 1
