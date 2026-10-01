@echo off
REM MHCOIN solo miner via terminal (same chain as Desktop: %USERPROFILE%\.mhcoin\mainnet)
REM
REM IMPORTANT: Stop mining in Desktop (or quit the app) first — only one process
REM may write the same datadir at a time.
REM
REM Usage:
REM   mine_mainnet.bat
REM   mine_mainnet.bat mhc1youraddress…
REM
REM Stop: Ctrl+C
setlocal EnableExtensions
cd /d "%~dp0\.."

if not defined MHCOIN_NETWORK set "MHCOIN_NETWORK=mainnet"
set "PYTHONPATH=%CD%;%PYTHONPATH%"

set "PY=python"
if exist "%CD%\.venv\Scripts\python.exe" set "PY=%CD%\.venv\Scripts\python.exe"

set "ADDR=%~1"
if "%ADDR%"=="" set "ADDR=%MHCOIN_MINER_ADDRESS%"
if "%ADDR%"=="" (
  echo MHCOIN terminal miner — network=%MHCOIN_NETWORK%
  echo Data: %%USERPROFILE%%\.mhcoin\%MHCOIN_NETWORK%
  echo Copy your address from Desktop -^> Receive, then paste below.
  echo.
  set /p ADDR=Reward address ^(mhc1...^): 
)
if "%ADDR%"=="" (
  echo No address — abort.
  exit /b 1
)

echo.
echo Starting solo miner...
echo   network: %MHCOIN_NETWORK%
echo   address: %ADDR%
echo   stop:    Ctrl+C
echo.

"%PY%" -m mhcoin.cli mining start --network %MHCOIN_NETWORK% --address %ADDR%
set "ERR=%ERRORLEVEL%"
echo.
pause
exit /b %ERR%
