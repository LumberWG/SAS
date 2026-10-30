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

# ---------- 0. Admin check ----------
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "[ERROR] Please run as Administrator" -ForegroundColor Red
    exit 1
}

# ---------- 0b. Transcript ----------
# 静默部署（计划任务 SASDeploy）没有窗口，输出同时落盘，事后可查
$Root   = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogDir = Join-Path $Root 'data\logs'

# 后端若在 .env 里配了 API_TOKEN，探活请求要带上令牌，否则会误报 401。
# 用法：先在当前会话 $env:SAS_API_TOKEN = '<token>'，未设置则不带任何头。
$ApiHeaders = @{}
if ($env:SAS_API_TOKEN) { $ApiHeaders['X-API-Token'] = $env:SAS_API_TOKEN }
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
try { Start-Transcript -Path (Join-Path $LogDir 'deploy.log') -Append -Force | Out-Null } catch { }

# ---------- 1. Build frontend ----------
# NOTE: do NOT set $ErrorActionPreference='Stop' globally - npm emits build
# warnings on stderr which PowerShell 5.1 would treat as a terminating
# NativeCommandError and abort the pipeline. Use explicit exit-code checks.
Write-Host "[BUILD] Building frontend (npm run build)..."

# Entry-chunk hash BEFORE build, used to prove the bundle actually changed.
function Get-EntryHash {
    param([string]$DistDir)
    $idx = Join-Path $DistDir 'index.html'
    if (-not (Test-Path $idx)) { return '(none)' }
    $m = Select-String -Path $idx -Pattern 'assets/index-([A-Za-z0-9_-]+)\.js' -ErrorAction SilentlyContinue |
         Select-Object -First 1
    if ($m) { return $m.Matches[0].Groups[1].Value }
    return '(unknown)'
}
$DistDir  = 'e:\Study\SAS\frontend\dist'
$entryOld = Get-EntryHash $DistDir
Write-Host ("[BUILD] Current entry: index-{0}.js" -f $entryOld)

Push-Location e:\Study\SAS\frontend
$buildOk = $true
try {
    & npm run build 2>&1 | ForEach-Object { $_.ToString() }
    if ($LASTEXITCODE -ne 0) {
        $buildOk = $false
        Write-Host ("[BUILD] npm run build failed (exit {0})" -f $LASTEXITCODE) -ForegroundColor Red
    }
} catch {
    $buildOk = $false
    Write-Host ("[BUILD] exception: {0}" -f $_.Exception.Message) -ForegroundColor Red
} finally {
    Pop-Location
}
if (-not $buildOk) {
    Write-Host "[BUILD] Aborted - fix build errors and re-run." -ForegroundColor Red
    exit 1
}
Write-Host "[BUILD] Done -> frontend/dist" -ForegroundColor Green

# ---------- 1b. Verify the built bundle matches index.html ----------
# Guards against the 2026-10-25 incident: source edited at 20:24 but dist was
# still yesterday's build, so the site silently served stale code.
$entryNew = Get-EntryHash $DistDir
Write-Host ("[VERIFY] Entry: index-{0}.js -> index-{1}.js" -f $entryOld, $entryNew)
if ($entryOld -ne $entryNew) {
    Write-Host "[VERIFY] Entry changed - new bundle will be served" -ForegroundColor Green
} else {
    Write-Host "[VERIFY] Entry unchanged (source may not have changed)" -ForegroundColor Yellow
}

$refs = Select-String -Path (Join-Path $DistDir 'index.html') -Pattern '(?:src|href)="(/assets/[^"]+)"' -AllMatches |
        ForEach-Object { $_.Matches } |
        ForEach-Object { $_.Groups[1].Value } |
        Sort-Object -Unique
$missing = @()
foreach ($r in $refs) {
    if (-not (Test-Path (Join-Path $DistDir $r.TrimStart('/')))) { $missing += $r }
}
if ($missing.Count -gt 0) {
    Write-Host "[VERIFY] index.html references missing assets:" -ForegroundColor Red
    $missing | ForEach-Object { Write-Host ("  " + $_) -ForegroundColor Red }
    exit 1
}
Write-Host ("[VERIFY] All {0} referenced assets present" -f $refs.Count) -ForegroundColor Green

# ---------- 2. Restart SAS service ----------
Write-Host "[RESTART] Restarting SAS service..."
# 确保 nssm 停止服务时连同子进程(uvicorn)一起终止。否则旧进程残留占用
# 8000 端口会导致新进程绑定失败，线上继续跑旧代码(且 overview 仍慢)。
$nssm = (Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter nssm.exe -ErrorAction SilentlyContinue | Select-Object -First 1).FullName
if ($nssm) {
    & $nssm set SAS AppKillProcessTree 1 2>&1 | ForEach-Object { $_.ToString() } | Out-Null
}
Stop-Service SAS -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3
Start-Service SAS -ErrorAction SilentlyContinue
Write-Host "[RESTART] Service restarted" -ForegroundColor Green

# Frontend service loads serve_frontend.py (proxy + cache headers); restart it so
# edits to that file take effect. Changes under frontend/dist need no restart.
Restart-Service SAS-frontend -Force -ErrorAction SilentlyContinue
Write-Host "[RESTART] Frontend service restarted" -ForegroundColor Green

# ---------- 3. Verify (poll health up to 60s) ----------
$health = $null
for ($i = 0; $i -lt 12; $i++) {
    Start-Sleep -Seconds 5
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/health" -UseBasicParsing -TimeoutSec 10 -Headers $ApiHeaders
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

# Business API readiness: /api/health responding does NOT guarantee the
# backend is usable. On 2026-10-23 the event loop was blocked by a sync
# data-source call, so health returned 200 but every business API timed out.
# Probe a few cheap (DB-only) endpoints to catch that class of failure.
$bizPaths = @("/api/paper/account", "/api/screening/strategies", "/api/analysis/status")
$bizOk = $true
foreach ($p in $bizPaths) {
    try {
        $sw = Measure-Command { $br = Invoke-WebRequest -Uri ("http://127.0.0.1:8000" + $p) -UseBasicParsing -TimeoutSec 15 -Headers $ApiHeaders }
        if ($br.StatusCode -ge 500) {
            $bizOk = $false
            Write-Host ("Business  : {0} -> HTTP {1} [DEGRADED]" -f $p, $br.StatusCode) -ForegroundColor Yellow
        } else {
            Write-Host ("Business  : {0} -> HTTP {1} ({2}ms)" -f $p, $br.StatusCode, [int]$sw.TotalMilliseconds) -ForegroundColor Green
        }
    } catch {
        $bizOk = $false
        Write-Host ("Business  : {0} -> TIMEOUT/ERR (event loop may be blocked)" -f $p) -ForegroundColor Red
    }
}
if (-not $bizOk) {
    Write-Host "[WARN] Some business APIs not ready - backend may be blocked or data-source degraded." -ForegroundColor Yellow
}

# Public URL check (fixed subdomain; change if yours differs)
# 2026-10-25: mysas tunnel is pinned to region cn_top -> *.cpolar.top
$PublicUrl = "https://mysas.cpolar.top"
# NOTE: Invoke-WebRequest from an *elevated* PS 5.1 session fails the TLS
# handshake against cpolar ("The underlying connection was closed") even with
# Tls12 forced and all certs trusted; curl.exe succeeds in both elevated and
# normal sessions. Use curl.exe here.
$pubCode = ''
if (Get-Command curl.exe -ErrorAction SilentlyContinue) {
    $pubCode = & curl.exe -s -o NUL -w '%{http_code}' --max-time 20 ($PublicUrl + '/api/health')
}
if ($pubCode -eq '200') {
    Write-Host ("Public    : HTTP 200 ({0})" -f $PublicUrl) -ForegroundColor Green
} elseif ($pubCode) {
    Write-Host ("Public    : HTTP {0} ({1})" -f $pubCode, $PublicUrl) -ForegroundColor Yellow
    Write-Host "(cpolar tunnel may be down; check cpolar service)"
} else {
    Write-Host ("Public    : SKIPPED ({0}) - curl.exe not found" -f $PublicUrl) -ForegroundColor Yellow
}
Write-Host "=========================================="
Write-Host "Deploy complete. Remember: git commit/push is manual."
