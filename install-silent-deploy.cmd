@echo off
REM 一次性安装：注册静默任务 SASDeploy（构建+重启）与 SASRestart（只重启后端）
REM 双击本文件，在 UAC 弹窗点“是”；安装完成后此文件不再需要。
REM 装好后：双击 restart-backend.vbs / deploy-silent.vbs 均无窗口、无 UAC。
setlocal
set "PS1=%~dp0install-silent-deploy.ps1"
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','%PS1%'"
endlocal
