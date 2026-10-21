@echo off
rem Double-click entry for Windows: runs install.ps1 and keeps the window open.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
pause
