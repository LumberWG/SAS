@echo off
REM Daily T-candidate cross-sectional signal runner (Windows Task Scheduler target).
REM Default data dir = SAS_DATA_DIR in app.config (D:\Backup\SAS\data). To switch source,
REM set it above, e.g.:  set SAS_DATA_DIR=E:\Study\SAS\data
cd /d E:\Study\SAS\backend
.venv\Scripts\python.exe scripts\twin_t_daily_signal.py %*
if errorlevel 1 (
    echo [run_daily_signal] FAILED, see error above
    exit /b 1
)
echo [run_daily_signal] DONE. Report: data\logs\twin_t_signal_YYYY-MM-DD.md
