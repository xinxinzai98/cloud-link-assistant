@echo off
echo Starting the cloud relay at 127.0.0.1:18443
"%~dp0python\python.exe" -B "%~dp0windows_bridge.py" cloud --port 18443
if errorlevel 1 pause
