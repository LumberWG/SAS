@echo off
chcp 65001 >nul
REM ============================================================
REM  分钟线日更（本地 fsdb -> parquet），每交易日 17:30 由计划任务
REM  SASMinutesFsdb 调用；也可手动双击补数据。
REM
REM  为什么需要它：个股详情的分时图读本地 1 分钟库，而日 K 由
REM  16:00 的预热任务维护。分钟线此前只能手动跑，忘一天就会
REM  「报价是今天、分时图停在昨天」，页面标注「本地历史 · 某日」。
REM
REM  增量逻辑在 fetch_minutes_fsdb.py：按文件最后一根日期判断，
REM  落后的只补增量；已最新则整只跳过（清单存在 _done_<freq>.txt）。
REM  手动只补某一天：.venv\Scripts\python.exe scripts\run_minutes_all.py --end 20261030
REM ============================================================
setlocal
cd /d "E:\Study\SAS\backend"

set LOG=logs\minutes_daily.log
if not exist logs mkdir logs

REM 同一时刻只允许一个实例（两个进程写同一批 parquet 会互相覆盖）
powershell -NoProfile -Command "if (Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*run_minutes_all*' -or $_.CommandLine -like '*fetch_minutes_fsdb*' }) { exit 1 } else { exit 0 }"
if %errorlevel%==1 (
    echo [%date% %time%] SKIP: 已有导出进程在跑 >> "%LOG%"
    exit /b 0
)

echo [%date% %time%] START >> "%LOG%"
.venv\Scripts\python.exe scripts\run_minutes_all.py >> "%LOG%" 2>&1
set RC=%errorlevel%
echo [%date% %time%] END exitcode=%RC% >> "%LOG%"
exit /b %RC%
