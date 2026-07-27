#!/usr/bin/env bash
# MarkItDown — macOS start (port of start.bat). Requires: uv (for a Python 3.12 venv).
# Creates .venv on first run, installs deps, runs diagnostics, launches server.py, waits for health.
set -uo pipefail
cd "$(dirname "$0")"

APP_NAME="MarkItDown"
APP_HOST="127.0.0.1"
APP_PORT="8000"
APP_URL="http://${APP_HOST}:${APP_PORT}"
VENV_PY=".venv/bin/python"
PID_FILE=".server.pid"
STAMP_FILE=".deps_installed"

health_ok() { curl -fsS --max-time 2 "${APP_URL}/api/health" 2>/dev/null | grep -q '"status"'; }

# Already up?
if health_ok; then
  echo "[OK] ${APP_NAME} already running at ${APP_URL}"
  command -v open >/dev/null && open "${APP_URL}" || true
  exit 0
fi

# venv (uv → Python 3.12)
if [ ! -x "$VENV_PY" ]; then
  echo "Creating virtual environment (uv, Python 3.12)..."
  uv venv --python 3.12 .venv || { echo "[ERROR] venv create failed — need uv + Python 3.12."; exit 1; }
fi

# deps (reinstall when stamp missing or older than requirements.txt)
if [ ! -f "$STAMP_FILE" ] || [ "requirements.txt" -nt "$STAMP_FILE" ]; then
  echo "Installing dependencies into .venv (first run can be slow)..."
  uv pip install --python "$VENV_PY" -r requirements.txt || { echo "[ERROR] dependency install failed."; exit 1; }
  date > "$STAMP_FILE"
fi

# diagnostics
if [ "${MD_CREATOR_SKIP_DOCTOR:-0}" != "1" ] && [ -f tools/doctor.py ]; then
  echo "Running startup diagnostics..."
  if ! "$VENV_PY" tools/doctor.py --compact > .doctor.json 2> .doctor.err.log; then
    echo "[ERROR] diagnostics failed — see .doctor.json and .doctor.err.log"; exit 1
  fi
  echo "[OK] diagnostics passed (.doctor.json)."
fi

# stop any prior instance, then confirm the port is free
./stop.sh --quiet >/dev/null 2>&1 || true
if lsof -nP -iTCP:"${APP_PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "[ERROR] port ${APP_PORT} is in use. Free it and re-run."; exit 1
fi

# launch (backgrounded, detached)
echo "Starting ${APP_NAME} at ${APP_URL}..."
APP_NAME="$APP_NAME" APP_HOST="$APP_HOST" APP_PORT="$APP_PORT" APP_RELOAD=0 \
  nohup "$VENV_PY" server.py > .server.log 2> .server.err.log &
echo $! > "$PID_FILE"

# health poll (~15s)
for _ in $(seq 1 30); do
  if health_ok; then
    echo "[OK] ${APP_NAME} healthy at ${APP_URL}"
    command -v open >/dev/null && open "${APP_URL}" || true
    echo "Run ./stop.sh to close it."
    exit 0
  fi
  sleep 0.5
done
echo "[ERROR] server did not become healthy — see .server.log and .server.err.log"
./stop.sh --quiet >/dev/null 2>&1 || true
exit 1
