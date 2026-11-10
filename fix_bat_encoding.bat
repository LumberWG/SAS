@echo off
REM Normalize all .bat/.cmd/.ps1 in the project to UTF-8-BOM + CRLF.
REM This fixes the "Chinese characters not recognized" batch-file error.
REM Pure ASCII so this launcher itself never has the encoding problem.
cd /d "E:\Study\SAS\backend"
.venv\Scripts\python.exe scripts\fix_bat_encoding.py %*
