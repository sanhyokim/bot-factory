@echo off
rem Run the factory once (called every day by Windows Task Scheduler).
rem Output is also saved under data\logs\.
cd /d "%~dp0"
set PYTHONUTF8=1
".venv\Scripts\python.exe" -m factory run-daily
