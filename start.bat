@echo off
rem Double-click: open the local workbench in the browser (127.0.0.1 only).
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Not installed yet. Double-click install.bat first.
  pause
  exit /b 1
)
echo qwb workbench: http://127.0.0.1:8765   (close this window to stop)
".venv\Scripts\python.exe" -X utf8 -m qwb.cli ui
pause
