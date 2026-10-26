@echo off
rem Starts Charlotte's helper, which starts The Witcher 3 with the settings the mod needs.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Run setup.bat first.
  pause
  exit /b
)
rem Picks up any library a newer version of the helper needs (quick when nothing is missing).
.venv\Scripts\python -m pip install --quiet --disable-pip-version-check -r requirements.txt
.venv\Scripts\python charlotte_helper.py
pause
