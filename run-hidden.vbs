' 通用包装：无窗口运行一个 PowerShell 脚本（供计划任务调用）
' 用法：wscript.exe run-hidden.vbs "E:\Study\SAS\xxx.ps1"
'
' 为什么用 wscript 包一层：
'   Task Scheduler 直接启动 powershell.exe（控制台程序）时，即使加
'   -WindowStyle Hidden，也会先创建控制台窗口再隐藏（可见一闪）。
'   wscript.exe 是 GUI 子系统程序，本身没有控制台；由它用窗口样式 0
'   （SW_HIDE）拉起 powershell，才是真正的无窗口。
Option Explicit

Dim shell, args, ps1
Set args = WScript.Arguments
If args.Count < 1 Then WScript.Quit 2

ps1 = args(0)
Set shell = CreateObject("WScript.Shell")
' 0 = 隐藏窗口，True = 等待脚本结束后再退出（保持任务状态准确）
shell.Run "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & ps1 & """", 0, True
WScript.Quit 0
