# SAS production update script (ASCII only for PowerShell 5.1 compatibility)
# Usage (Administrator required):
#   powershell -ExecutionPolicy Bypass -File e:\Study\SAS\deploy.ps1
#
# Run this after code changes:
#   1. Build frontend (npm run build -> frontend/dist)
#   2. Restart SAS service (loads new backend code)
#   3. Poll health check (local + public via cpolar)
#
# It does NOT git commit/push - do that separately.

$ErrorActionPreference = "Stop"

# ---------- 0. Admin check ----------
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "[ERROR] Please run as Administrator" -ForegroundColor Red
    exit 1
}

# ---------- 1. Build frontend ----------
Write-Host "[BUILD] Building frontend (npm run build)..."
Push-Location e:\Study\SAS\frontend
try {
    npm run build 2>&1 | Select-Object -Last 5
    if ($LASTEXITCODE -ne 0) { throw "npm run build failed (exit $LASTEXITCODE)" }
} finally {
    Pop-Location
}
Write-Host "[BUILD] Done -> frontend/dist" -ForegroundColor Green

# ---------- 2. Restart SAS service ----------
Write-Host "[RESTART] Restarting SAS service..."
Restart-Service SAS
Write-Host "[RESTART] Service restarted" -ForegroundColor Green

# ---------- 3. Verify (poll health up to 60s) ----------
$health = $null
for ($i = 0; $i -lt 12; $i++) {
    Start-Sleep -Seconds 5
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/health" -UseBasicParsing -TimeoutSec 10
        $health = $r.StatusCode
        break
    } catch {
        # keep polling
    }
}
Write-Host ""
Write-Host "================= VERIFY ================="
if ($health) {
    Write-Host ("Local     : HTTP {0}" -f $health) -ForegroundColor Green
} else {
    Write-Host "Local     : FAILED after 60s (log: data\logs\sas-service-error.log)" -ForegroundColor Red
}

# Public URL check (fixed subdomain; change if yours differs)
$PublicUrl = "https://mysas.vip.cpolar.cn"
# PS 5.1: Invoke-WebRequest has no -SkipCertificateCheck; trust all certs for this check
[Net.ServicePointManager]::ServerCertificateValidationCallback = { $true }
try {
    $r2 = Invoke-WebRequest -Uri "$PublicUrl/api/health" -UseBasicParsing -TimeoutSec 20
    Write-Host ("Public    : HTTP {0} ({1})" -f $r2.StatusCode, $PublicUrl) -ForegroundColor Green
} catch {
    Write-Host ("Public    : FAILED - {0}" -f $_.Exception.Message) -ForegroundColor Yellow
    Write-Host "(cpolar tunnel may be down; check cpolar service)"
}
Write-Host "=========================================="
Write-Host "Deploy complete. Remember: git commit/push is manual."
