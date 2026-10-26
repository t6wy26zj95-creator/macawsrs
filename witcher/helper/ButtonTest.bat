@echo off
rem Shows the name of each controller button you press, to pick the mute button.
cd /d "%~dp0"
.venv\Scripts\python charlotte_helper.py --buttons
pause
