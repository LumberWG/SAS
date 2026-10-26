# SAS services restart (silent; executed by scheduled task SASRestart)
# Restarts:
#   SAS          - backend API  (uvicorn, :8000)
#   SAS-frontend - static dist + /api reverse proxy (:8080)
# Usage:  schtasks /run /tn SASRestart     (no UAC prompt, no window)
#         or double-click restart-backend.vbs
# Log:    data\logs\restart.log
# ASCII only: PowerShell 5.1 reads BOM-less files as ANSI.
$Root   = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogDir = Join-Path $Root 'data\logs'
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
$LogFile = Join-Path $LogDir 'restart.log'

function Write-Log {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
}

# ---------- 0. Elevation check ----------
$identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Log 'ERROR: not elevated - abort. Register task SASRestart first.'
    exit 1
}

Write-Log 'restart requested (backend + frontend)'

# ---------- 1. Restart backend ----------
# AppKillProcessTree: nssm must kill the uvicorn child too, otherwise the old
# process keeps holding port 8000 and the new one cannot bind (runs stale code).
$nssm = (Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter nssm.exe -ErrorAction SilentlyContinue | Select-Object -First 1).FullName
if ($nssm) {
    & $nssm set SAS AppKillProcessTree 1 2>&1 | Out-Null
}

Stop-Service SAS -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
Start-Service SAS -ErrorAction SilentlyContinue
Write-Log 'SAS (backend :8000) restarted'

# ---------- 2. Restart frontend proxy ----------
# Needed when serve_frontend.py changes (headers / proxy logic); dist changes
# alone do not require it, but restarting is cheap and keeps both in sync.
$frontend = Get-Service SAS-frontend -ErrorAction SilentlyContinue
if ($frontend) {
    Restart-Service SAS-frontend -Force -ErrorAction SilentlyContinue
    Write-Log 'SAS-frontend (:8080) restarted'
} else {
    Write-Log 'SAS-frontend service not found - skipped'
}

# ---------- 3. Health check ----------
$backendOk  = $false
$frontendOk = -not [bool]$frontend
for ($i = 0; $i -lt 12; $i++) {
    Start-Sleep -Seconds 5
    if (-not $backendOk) {
        try {
            $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/api/health' -UseBasicParsing -TimeoutSec 10
            if ($r.StatusCode -eq 200) { $backendOk = $true }
        } catch {
            # keep polling
        }
    }
    if (-not $frontendOk) {
        try {
            $f = Invoke-WebRequest -Uri 'http://127.0.0.1:8080/' -UseBasicParsing -TimeoutSec 10
            if ($f.StatusCode -eq 200) { $frontendOk = $true }
        } catch {
            # keep polling
        }
    }
    if ($backendOk -and $frontendOk) { break }
}

if ($backendOk)  { Write-Log 'backend health OK (http://127.0.0.1:8000/api/health)' }
else             { Write-Log 'backend health FAILED after 60s - see data\logs\sas-service-error.log' }

if ($frontendOk) { Write-Log 'frontend health OK (http://127.0.0.1:8080/)' }
else             { Write-Log 'frontend health FAILED - see data\logs\frontend-error.log' }

if ($backendOk -and $frontendOk) { exit 0 }
exit 1
