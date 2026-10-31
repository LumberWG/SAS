# 一次性安装：注册静默部署任务 SASDeploy（必须以管理员身份运行）
# 装好之后，双击 deploy-silent.vbs 即可部署，不弹窗口、不弹 UAC。
# 日志：D:\Backup\SAS\data\logs\deploy.log
$ErrorActionPreference = 'Stop'

# 提权窗口一闪而过，输出同时落盘，失败时可直接查看
$Root   = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogDir = Join-Path $Root 'data\logs'
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
try { Start-Transcript -Path (Join-Path $LogDir 'install-tasks.log') -Append -Force | Out-Null } catch { }

$identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host '[ERROR] 请以管理员身份运行本脚本（右键 -> 以管理员身份运行）' -ForegroundColor Red
    exit 1
}

$TaskName = 'SASDeploy'
$Root     = Split-Path -Parent $MyInvocation.MyCommand.Path
$Deploy   = Join-Path $Root 'deploy.ps1'

if (-not (Test-Path $Deploy)) {
    Write-Host ("[ERROR] 找不到 " + $Deploy) -ForegroundColor Red
    exit 1
}

# 用 wscript + run-hidden.vbs 包装：Task Scheduler 直接起 powershell.exe
# 时仍会创建控制台窗口（-WindowStyle Hidden 只是先显示再隐藏），包一层才真无窗口
$Wrapper = Join-Path $Root 'run-hidden.vbs'
$action = New-ScheduledTaskAction -Execute 'wscript.exe' `
    -Argument ('"' + $Wrapper + '" "' + $Deploy + '"')

# 以当前用户身份 + 最高权限运行；LogonType=Interactive 不需要保存密码
$runAs = New-ScheduledTaskPrincipal -UserId ($env:USERDOMAIN + '\' + $env:USERNAME) `
    -LogonType Interactive -RunLevel Highest

# Hidden：任务本身不显示窗口；无触发器（仅手动/脚本触发）
$settings = New-ScheduledTaskSettingsSet -Hidden -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Principal $runAs `
    -Settings $settings -Force `
    -Description 'SAS 静默部署：构建前端 + 重启服务 + 健康检查（无窗口、无 UAC）' | Out-Null

Write-Host ('[OK] 已注册计划任务 ' + $TaskName) -ForegroundColor Green
Write-Host ('     入口：' + (Join-Path $Root 'deploy-silent.vbs') + '（双击即可，无窗口）') -ForegroundColor Gray
Write-Host ('     日志：' + (Join-Path $Root 'data\logs\deploy.log')) -ForegroundColor Gray
Write-Host ('     手动触发：schtasks /run /tn ' + $TaskName) -ForegroundColor Gray

# ---------- 附加：只重启后端的轻量任务（不构建前端） ----------
$RestartTaskName = 'SASRestart'
$RestartScript   = Join-Path $Root 'restart-backend.ps1'
if (Test-Path $RestartScript) {
    $restartAction = New-ScheduledTaskAction -Execute 'wscript.exe' `
        -Argument ('"' + $Wrapper + '" "' + $RestartScript + '"')
    Register-ScheduledTask -TaskName $RestartTaskName -Action $restartAction -Principal $runAs `
        -Settings $settings -Force `
        -Description 'SAS 静默重启后端：仅重启 SAS 服务 + 健康检查（无窗口、无 UAC）' | Out-Null

    Write-Host ('[OK] 已注册计划任务 ' + $RestartTaskName) -ForegroundColor Green
    Write-Host ('     入口：' + (Join-Path $Root 'restart-backend.vbs') + '（双击即可，无窗口）') -ForegroundColor Gray
    Write-Host ('     日志：' + (Join-Path $Root 'data\logs\restart.log')) -ForegroundColor Gray
    Write-Host ('     手动触发：schtasks /run /tn ' + $RestartTaskName) -ForegroundColor Gray
} else {
    Write-Host ('[WARN] 未找到 ' + $RestartScript + '，跳过 SASRestart 注册') -ForegroundColor Yellow
}
