@echo off
cd /d "%~dp0"
python -m pip install -q -r requirements.txt
set EMBEDDED_WORKERS=true
echo Open http://localhost:8000 in your browser. Press Ctrl+C to stop.
start "" http://localhost:8000
python -m uvicorn engine.web.app:app --port 8000
pause
