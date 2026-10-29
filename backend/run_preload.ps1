# Launch the minute5 preloader (pure ASCII on purpose: cmd breaks on non-ASCII paths).
#
# --proxy is REQUIRED as of 2026-10-29 20:00: the direct (home) IP got banned by
# Baostock (error 10001011) after ~14 min at ~25 req/min. The SOCKS5 proxy uses a
# different egress IP and still logs in fine.
# shards 4 -> 3 and sleep 1.0 -> 2.5 pull the request rate back to the previously
# safe level (~15 req/min) so the proxy egress does not get banned too.
Set-Location -Path $PSScriptRoot
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

& ".\.venv\Scripts\python.exe" "scripts\preload_minutes_parallel.py" `
    --proxy --shards 4 --sleep 2.5 --stall-timeout 5 --max-restarts 50

Write-Host ""
Write-Host "Preloader exited. Press Enter to close."
Read-Host
