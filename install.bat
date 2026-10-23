@echo off
chcp 65001 >nul
rem 双击安装 qwb（大约 3–10 分钟，需要联网）
echo 正在安装 qwb，请保持网络连接，不要关闭这个窗口……
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set "INSTALL_EXIT=%ERRORLEVEL%"
echo.
pause
exit /b %INSTALL_EXIT%
