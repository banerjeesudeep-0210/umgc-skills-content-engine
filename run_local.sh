#!/bin/bash
# Runs the whole engine on this computer: web dashboard plus every worker pool, with a local SQLite database.
cd "$(dirname "$0")"
PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
$PY -m pip install -q -r requirements.txt || $PY -m pip install -q --user -r requirements.txt
[ -f .env ] && set -a && . ./.env && set +a
export EMBEDDED_WORKERS=true
echo "Open http://localhost:8000 in your browser. Press Ctrl+C to stop."
( sleep 3; command -v open >/dev/null && open http://localhost:8000 ) &
$PY -m uvicorn engine.web.app:app --port 8000
