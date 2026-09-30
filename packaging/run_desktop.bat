@echo off
REM MHCOIN Core Desktop — Windows double-click launcher
cd /d "%~dp0\.."
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_desktop.ps1" %*
