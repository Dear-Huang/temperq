@echo off
chcp 65001 >nul
rem 双击打开 qwb 工作台（只在本机，浏览器访问 127.0.0.1）
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 还没有安装。请先双击 install.bat
  pause
  exit /b 1
)
echo 正在启动 TemperQ 工作台，浏览器会自动打开。
echo 使用期间不要关闭这个窗口；关闭它就退出工作台。
".venv\Scripts\python.exe" -X utf8 -m qwb.cli ui %*
pause
