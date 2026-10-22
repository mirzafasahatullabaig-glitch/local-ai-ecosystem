@echo off
rem Stops the station started by start-station.bat: whatever listens on :8000,
rem with its process tree (the venv python is a launcher with a child).
set found=
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":8000 .*LISTENING"') do (
  set found=1
  taskkill /PID %%p /T /F >nul 2>&1
)
if defined found (echo eco station stopped) else (echo eco station was not running)
