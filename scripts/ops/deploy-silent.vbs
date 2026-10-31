' 静默部署入口：双击即可，不弹窗口、不弹 UAC
' 原理：计划任务 SASDeploy 以最高权限注册，这里只是"触发"它，
'       WScript.Shell.Run 的窗口样式 0 = 隐藏，第三个参数 False = 不等待。
' 日志：D:\Backup\SAS\data\logs\deploy.log
Option Explicit

Dim sh
Set sh = CreateObject("WScript.Shell")
sh.Run "schtasks.exe /run /tn SASDeploy", 0, False
Set sh = Nothing
