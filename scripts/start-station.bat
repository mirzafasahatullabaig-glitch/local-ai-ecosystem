@echo off
rem Starts the laptop "brain": FastAPI + MQTT listener + Whisper, reachable by
rem the Pi on :8000. Loads ECO_MQTT_USER / ECO_MQTT_PASS / ECO_API_KEY from
rem .env.mqtt (gitignored). Output goes to api.log. Runs at logon via a
rem shortcut in the Startup folder.
cd /d "%~dp0.."

rem One instance only: two servers share the MQTT client id and knock each other off.
netstat -ano | findstr /r /c:":8000 .*LISTENING" >nul && (
  echo eco station already running on :8000
  exit /b 0
)

for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env.mqtt") do set "%%a=%%b"

".venv\Scripts\python.exe" -m uvicorn backend.api.main:app --host 0.0.0.0 --port 8000 >> api.log 2>&1
