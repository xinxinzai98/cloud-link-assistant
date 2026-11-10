@echo off
echo Starting the model relay at 127.0.0.1:19847
"%~dp0python\python.exe" -B "%~dp0windows_bridge.py" model --port 19847 --proxy-port 7890
if errorlevel 1 pause
