# Register SAS backend as a Windows service (auto-start on boot + auto-restart on crash)
# Usage (Administrator required):
#   powershell -ExecutionPolicy Bypass -File e:\Study\SAS\install_service.ps1
#
# What it does:
#   - Registers 'SAS' service via NSSM (uvicorn production mode, serves frontend dist)
#   - Start type = Automatic (boot auto-start)
#   - Auto restart on process exit (5s throttle to avoid crash loops)
#   - Logs to data/logs/ (10MB rotation)
#   - Configures cpolar service failure recovery (1/2/5 min restarts)
#
# NOTE: ASCII only on purpose - Windows PowerShell 5.1 decodes .ps1 as GBK
# by default; non-ASCII characters break the parser.

$ErrorActionPreference = "Stop"
$Python = "e:\Study\SAS\backend\.venv\Scripts\python.exe"
$WorkDir = "e:\Study\SAS\backend"
$LogDir = "D:\Backup\SAS\data\logs"

# ---------- 0. Admin check ----------
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "[ERROR] Please run as Administrator" -ForegroundColor Red
    exit 1
}

# ---------- 1. Log directory ----------
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# ---------- 2. Register / refresh SAS service ----------
$svc = Get-Service -Name "SAS" -ErrorAction SilentlyContinue
if (-not $svc) {
    Write-Host "[INSTALL] Registering SAS service..."
    nssm install SAS $Python "-m uvicorn app.main:app --host 0.0.0.0 --port 8000"
} else {
    Write-Host "[UPDATE] SAS service exists, refreshing config..."
}

nssm set SAS AppDirectory $WorkDir
nssm set SAS DisplayName "SAS Stock Analysis System"
nssm set SAS Description "A-share analysis backend: quotes/screening/backtest/LLM/auto-monitor; serves frontend dist on port 8000"
nssm set SAS Start SERVICE_AUTO_START
nssm set SAS AppExit Default Restart      # any exit code -> auto restart
nssm set SAS AppRestartMethod 0           # 0 = restart the service itself
nssm set SAS AppThrottle 5000             # 5s restart interval, anti crash-loop
nssm set SAS AppStdout "$LogDir\sas-service.log"
nssm set SAS AppStderr "$LogDir\sas-service-error.log"
nssm set SAS AppRotateFiles 1
nssm set SAS AppRotateOnline 1
nssm set SAS AppRotateBytes 10485760      # rotate at 10MB per log file

# ---------- 3. cpolar service failure recovery ----------
Write-Host "[CONFIG] cpolar failure recovery..."
sc.exe failure cpolar actions= restart/60000/restart/120000/restart/300000 reset= 86400

# ---------- 4. Start service ----------
Start-Service SAS

# ---------- 5. Verify (poll health up to 60s) ----------
$s = Get-Service SAS
$startMode = (Get-CimInstance Win32_Service -Filter "Name='SAS'").StartMode
$health = $null
for ($i = 0; $i -lt 12; $i++) {
    Start-Sleep -Seconds 5
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/health" -UseBasicParsing -TimeoutSec 10
        $health = $r.StatusCode
        break
    } catch {
        # keep polling (service start + warmup may take a few seconds)
    }
}
Write-Host ""
Write-Host "================= VERIFY ================="
Write-Host ("Service   : {0} (StartMode: {1})" -f $s.Status, $startMode)
if ($health) {
    Write-Host ("Health    : HTTP {0}" -f $health) -ForegroundColor Green
} else {
    Write-Host "Health    : FAILED after 60s" -ForegroundColor Red
    Write-Host "(Log: data\logs\sas-service-error.log)"
}
Write-Host "=========================================="
Write-Host ""
Write-Host "Done. Useful commands:" -ForegroundColor Cyan
Write-Host "  Restart   : Restart-Service SAS"
Write-Host "  Stop      : Stop-Service SAS"
Write-Host "  Logs      : Get-Content D:\Backup\SAS\data\logs\sas-service.log -Tail 50"
Write-Host "  Uninstall : nssm remove SAS confirm"
