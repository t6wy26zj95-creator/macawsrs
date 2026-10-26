@echo off
rem One-time setup for Charlotte's helper: installs Python (if needed) and the three libraries it uses.
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo Installing Python. Windows may ask for permission...
  winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
  echo.
  echo Python is installed. Close this window and double-click setup.bat once more.
  pause
  exit /b
)
echo Setting up Charlotte's helper...
py -3 -m venv .venv
.venv\Scripts\python -m pip install --quiet --upgrade pip
.venv\Scripts\python -m pip install --quiet -r requirements.txt
if errorlevel 1 (
  echo Something went wrong. Take a screenshot of this window and send it to Claude.
) else (
  echo All set. From now on, start the game with Charlotte.bat.
)
pause
