# SAS 后端注册为 Windows 服务（开机自启 + 崩溃自动重启）
# 用法：右键本文件 → 使用 PowerShell 运行（必须以管理员身份）
#
# 注册后：
#   - 开机自动启动（Start = AUTO）
#   - 进程崩溃/被误杀后自动重启（NSSM AppExit Default Restart）
#   - cpolar 隧道服务配置失败自动恢复
# 卸载：nssm remove SAS confirm

$ErrorActionPreference = "Stop"
$Python = "e:\Study\SAS\backend\.venv\Scripts\python.exe"
$WorkDir = "e:\Study\SAS\backend"
$LogDir = "e:\Study\SAS\data\logs"

# ---------- 0. 管理员检查 ----------
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "[错误] 请以管理员身份运行本脚本（右键 → 使用 PowerShell 运行）" -ForegroundColor Red
    exit 1
}

# ---------- 1. 日志目录 ----------
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# ---------- 2. 注册/更新 SAS 服务 ----------
$svc = Get-Service -Name "SAS" -ErrorAction SilentlyContinue
if (-not $svc) {
    Write-Host "[安装] 注册 SAS 服务..."
    nssm install SAS $Python "-m uvicorn app.main:app --host 0.0.0.0 --port 8000"
} else {
    Write-Host "[更新] SAS 服务已存在，刷新配置..."
}

nssm set SAS AppDirectory $WorkDir
nssm set SAS DisplayName "SAS A股分析系统"
nssm set SAS Description "A股智能分析系统后端（行情/选股/回测/LLM/自动盯盘），前端dist由本服务托管"
nssm set SAS Start SERVICE_AUTO_START
nssm set SAS AppExit Default Restart      # 进程退出（任何退出码）→ 自动重启
nssm set SAS AppRestartMethod 0           # 0 = 重启服务自身
nssm set SAS AppThrottle 5000             # 重启间隔 5 秒，防止疯狂崩溃循环
nssm set SAS AppStdout "$LogDir\sas-service.log"
nssm set SAS AppStderr "$LogDir\sas-service-error.log"
nssm set SAS AppRotateFiles 1
nssm set SAS AppRotateOnline 1
nssm set SAS AppRotateBytes 10485760      # 单个日志 10MB 后轮转

# ---------- 3. cpolar 隧道服务失败自动恢复 ----------
Write-Host "[配置] cpolar 服务失败恢复策略..."
sc.exe failure cpolar actions= restart/60000/restart/120000/restart/300000 reset= 86400

# ---------- 4. 启动服务 ----------
Start-Service SAS
Start-Sleep -Seconds 12

# ---------- 5. 验证 ----------
$s = Get-Service SAS
Write-Host ""
Write-Host "================= 验证结果 ================="
Write-Host ("服务状态 : {0}（启动类型: {1}）" -f $s.Status, (Get-CimInstance Win32_Service -Filter "Name='SAS'").StartMode)
try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/health" -UseBasicParsing -TimeoutSec 20
    Write-Host ("健康检查 : HTTP {0}" -f $r.StatusCode) -ForegroundColor Green
} catch {
    Write-Host ("健康检查 : 失败 - {0}" -f $_.Exception.Message) -ForegroundColor Red
    Write-Host "（启动预热股票池约需 10-30 秒，可稍后重试；日志见 data\logs\sas-service-error.log）"
}
Write-Host "============================================"
Write-Host ""
Write-Host "完成。常用命令：" -ForegroundColor Cyan
Write-Host "  重启服务 : Restart-Service SAS"
Write-Host "  停止服务 : Stop-Service SAS"
Write-Host "  查看日志 : Get-Content e:\Study\SAS\data\logs\sas-service.log -Tail 50"
Write-Host "  卸载服务 : nssm remove SAS confirm"
