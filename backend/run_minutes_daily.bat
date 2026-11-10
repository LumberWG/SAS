@echo off
chcp 65001 >nul
REM ============================================================
REM  每日分钟线导出（本地 fsdb -> parquet 落地）
REM  由任务计划程序「SASMinutesFsdb」在每个交易日 17:30 触发
REM  也可手动双击补数据
REM
REM  背景：个股分时图读本地 1 分钟库，日K 由 16:00 预热任务维护。
REM  若某天漏跑，分时图停在昨天，而行情/日K 已是最新。
REM
REM  增量逻辑见 fetch_minutes_fsdb.py：按每只文件最后一根 bar 的日期判断，
REM  只补落后的票；已最新的文件整只跳过（存在 _done_<freq>.txt 标记）。
REM  手动补指定一天：
REM    .venv\Scripts\python.exe scripts\run_minutes_all.py --end 20261030
REM ============================================================
setlocal
cd /d "E:\Study\SAS\backend"

set LOG=logs\minutes_daily.log
if not exist logs mkdir logs

REM Allow only one instance (two exporters writing the same parquet set collide)
powershell -NoProfile -Command "if (Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*run_minutes_all*' -or $_.CommandLine -like '*fetch_minutes_fsdb*' }) { exit 1 } else { exit 0 }"
if %errorlevel%==1 (
    echo [%date% %time%] SKIP: another export already running >> "%LOG%"
    exit /b 0
)

echo [%date% %time%] START >> "%LOG%"
.venv\Scripts\python.exe scripts\run_minutes_all.py >> "%LOG%" 2>&1
set RC=%errorlevel%
echo [%date% %time%] END exitcode=%RC% >> "%LOG%"
exit /b %RC%
