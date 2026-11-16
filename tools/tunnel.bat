@echo off
rem Lend local 8010 to the server: ssh -N -R 8010:127.0.0.1:8010 gb
chcp 65001 >nul
title bsk tunnel 8010 -^> gb
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -R 8010:127.0.0.1:8010 gb
echo tunnel closed. double click again to reopen.
pause