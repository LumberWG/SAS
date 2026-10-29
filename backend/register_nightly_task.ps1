# Register the nightly 5-min preload as a Windows Scheduled Task.
# Run this script AS ADMINISTRATOR in your own PowerShell:
#   Right-click PowerShell -> Run as Administrator, then:
#   cd E:\Study\SAS\backend
#   .\register_nightly_task.ps1
# (schtasks is blocked inside some sandboxes, so this uses Register-ScheduledTask.)

$TaskName = "SAS-PreloadMinutes"
$BatPath  = "E:\Study\SAS\backend\run_preload_nightly.bat"

# Remove any previous version, then (re)create
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

$action = New-ScheduledTaskAction -Execute $BatPath
# Daily at 20:00. Change the time here if you prefer another hour.
$trigger = New-ScheduledTaskTrigger -Daily -At "20:00"
# Runs only while you are logged on (so the Forest SOCKS proxy is available).
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 20) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force

Write-Host "Registered scheduled task: $TaskName"
Write-Host "  Trigger : Daily at 20:00 (while logged on)"
Write-Host "  Action  : $BatPath"
Write-Host "  Verify  : Get-ScheduledTask -TaskName '$TaskName'"
Write-Host "  Run now : Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "  Remove  : Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
