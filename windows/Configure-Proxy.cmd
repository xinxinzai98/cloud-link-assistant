@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
"%~dp0python\python.exe" -B "%~dp0windows_bridge.py" configure
pause
