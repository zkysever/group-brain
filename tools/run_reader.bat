@echo off
rem bsk reader (real browser fallback). Forwards 403 / JS-shell pages from the server.
chcp 65001 >nul
title bsk reader 8010
set BSK_AUTO_START=0
set PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe
if not exist "%PY%" set PY=python
"%PY%" "%~dp0bsk_reader.py"
pause