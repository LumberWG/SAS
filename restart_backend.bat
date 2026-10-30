@echo off
chcp 65001 >nul
title Restart SAS backend
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo [!] Administrator privileges required.
  echo     Right-click this file and choose "Run as administrator".
  pause
  exit /b 1
)
echo Stopping SAS backend ...
net stop SAS
timeout /t 2 /nobreak >nul
echo Starting SAS backend ...
net start SAS
timeout /t 6 /nobreak >nul
echo.
echo ---- health check ----
powershell -NoProfile -Command "try{$r=Invoke-WebRequest -Uri 'http://127.0.0.1:8000/api/health' -TimeoutSec 8 -UseBasicParsing; 'health HTTP ' + $r.StatusCode}catch{'health FAILED: ' + $_.Exception.Message}"
echo.
echo ---- intraday check (should be fast, local data) ----
powershell -NoProfile -Command "$t=Get-Date; try{$r=Invoke-WebRequest -Uri 'http://127.0.0.1:8000/api/market/stocks/600519/intraday' -TimeoutSec 15 -UseBasicParsing; $ms=[math]::Round(((Get-Date)-$t).TotalMilliseconds); 'intraday HTTP ' + $r.StatusCode + ' in ' + $ms + ' ms'}catch{'intraday FAILED: ' + $_.Exception.Message}"
echo.
echo Done. Backend now reads local minute data:
echo   5m: E:\Study\SAS\data\minute5_fsdb
echo   1m: E:\Study\SAS\data\minute1_fsdb
pause
