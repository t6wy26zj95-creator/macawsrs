@echo off
rem Starts Charlotte's helper, which starts The Witcher 3 with the settings the mod needs.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Run setup.bat first.
  pause
  exit /b
)
.venv\Scripts\python charlotte_helper.py
pause
