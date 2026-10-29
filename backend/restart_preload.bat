@echo off
setlocal
cd /d "%~dp0"

echo ==================================================
echo  Restart minute5 preload (fixed worker)
echo ==================================================
echo.

echo [1/3] Stopping old preload processes ...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_preload.ps1"

echo.
echo [2/3] Waiting for handles to release ...
timeout /t 3 /nobreak >nul

echo [3/3] Starting 4 shards in a new window ...
start "SAS preload" powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_preload.ps1"

echo.
echo Done. Check the new window titled "SAS preload".
timeout /t 5 /nobreak >nul
