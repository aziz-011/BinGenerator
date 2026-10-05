@echo off
cd /d "%~dp0"
where py >nul 2>nul && (py -3 -m bingen %*) || (python -m bingen %*)
pause
