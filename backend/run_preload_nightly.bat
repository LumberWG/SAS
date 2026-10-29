@echo off
REM ============================================================
REM  Nightly auto pre-download of 5-min bars (Baostock, sharded)
REM  Called by Windows Task Scheduler daily, or run manually.
REM  Output: logs/nightly_combined.log (summary) + logs/minute5_shard_*.log
REM ============================================================
setlocal
cd /d "E:\Study\SAS\backend"

set LOG=logs\nightly_combined.log
if not exist "logs" mkdir "logs"

REM ---- optional overrides via env: PRELOAD_SHARDS / PRELOAD_LIMIT ----
REM shards 3 + sleep 2.5: keeps request rate ~15/min. Direct connection got
REM IP-banned by Baostock (error 10001011) at ~25/min, so --proxy is mandatory.
set SHARDS=4
if defined PRELOAD_SHARDS set SHARDS=%PRELOAD_SHARDS%
set EXTRA=
if defined PRELOAD_LIMIT set EXTRA=--limit %PRELOAD_LIMIT%

REM ---- 1) prevent duplicate runs: skip if a preload process is already running ----
powershell -NoProfile -Command "if (Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*preload_minutes*' }) { exit 1 } else { exit 0 }"
if %errorlevel%==1 (
    echo [%date% %time%] SKIP: preload already running >> "%LOG%"
    exit /b 0
)

REM ---- 2) check Forest SOCKS proxy port 7891 reachable; skip if not ----
powershell -NoProfile -Command "try { $c=New-Object System.Net.Sockets.TcpClient; $c.Connect('127.0.0.1',7891); $c.Close(); exit 0 } catch { exit 2 }"
if %errorlevel%==2 (
    echo [%date% %time%] SKIP: Forest SOCKS port 7891 not reachable >> "%LOG%"
    exit /b 0
)

echo [%date% %time%] START nightly preload shards=%SHARDS% >> "%LOG%"
.venv\Scripts\python.exe scripts\preload_minutes_parallel.py --proxy --shards %SHARDS% --sleep 2.5 %EXTRA% >> "%LOG%" 2>&1
echo [%date% %time%] END exitcode=%errorlevel% >> "%LOG%"
exit /b %errorlevel%
