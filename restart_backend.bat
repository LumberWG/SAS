@echo off
rem  Restart SAS backend service and verify the intraday endpoint.
rem  (D: migration is already done; this is just a restart + self-check.)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0restart_backend.ps1"
