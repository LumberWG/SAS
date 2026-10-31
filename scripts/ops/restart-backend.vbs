' 静默重启后端：双击即可，不弹窗口、不弹 UAC
' 原理：计划任务 SASRestart 已以最高权限注册，这里只负责“触发”它；
'       WScript.Shell.Run 的窗口样式 0 = 隐藏，第三个参数 False = 不等待。
' 前提：先以管理员身份运行一次 install-silent-deploy.cmd 完成注册。
' 日志：e:\Study\SAS\data\logs\restart.log
Option Explicit

Dim sh
Set sh = CreateObject("WScript.Shell")
sh.Run "schtasks.exe /run /tn SASRestart", 0, False
Set sh = Nothing
