@echo off
chcp 65001 >nul
REM ============================================================
REM  一键导出「建仓理由复盘」报告
REM  用法：双击；或带参数，例如
REM        export_lot_reason.bat --code 600519
REM        export_lot_reason.bat --code 600519 --code 000001
REM  依赖 backend/.venv 的 python（含项目依赖）
REM ============================================================
cd /d E:/Study/SAS/backend

REM Use today's date in the output filename (avoid overwriting history)
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set TS=%%i
if not exist ..\reports mkdir ..\reports
set OUT=..\reports\lot_reason_report_%TS%.md

.venv\Scripts\python.exe scripts\export_lot_reason.py --out "%OUT%" %*

echo.
echo ============================================================
echo   建仓理由复盘报告已生成：
echo   %OUT%
echo ============================================================
pause
