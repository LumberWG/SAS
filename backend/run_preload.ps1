# Launch the minute5 preloader (pure ASCII on purpose: cmd breaks on non-ASCII paths).
Set-Location -Path $PSScriptRoot
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

& ".\.venv\Scripts\python.exe" "scripts\preload_minutes_parallel.py" `
    --shards 4 --sleep 1.0 --stall-timeout 5 --max-restarts 50

Write-Host ""
Write-Host "Preloader exited. Press Enter to close."
Read-Host
