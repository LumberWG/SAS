# Stop all preload_minutes worker/launcher processes (pure ASCII on purpose).
# Run this in YOUR OWN PowerShell/terminal - the sandbox cannot kill other sessions' processes.

$procs = Get-CimInstance Win32_Process -Filter "name='python.exe'" |
         Where-Object { $_.CommandLine -like '*preload_minutes*' }

if (-not $procs) {
    Write-Host "No preload_minutes process found."
    exit 0
}

foreach ($p in $procs) {
    try {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
        Write-Host ("  killed PID " + $p.ProcessId)
    } catch {
        Write-Host ("  FAILED PID " + $p.ProcessId + " : " + $_.Exception.Message)
    }
}

Start-Sleep -Seconds 2
$left = Get-CimInstance Win32_Process -Filter "name='python.exe'" |
        Where-Object { $_.CommandLine -like '*preload_minutes*' }
if ($left) {
    Write-Host ""
    Write-Host "Still alive:"
    $left | ForEach-Object { Write-Host ("  PID " + $_.ProcessId) }
    Write-Host "Please press Ctrl+C in their console window, then run this again."
} else {
    Write-Host "All preload processes stopped. Cached data is kept, rerun will resume."
}
