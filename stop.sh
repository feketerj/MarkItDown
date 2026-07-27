#!/usr/bin/env bash
# MarkItDown — macOS stop (port of stop.bat). Usage: ./stop.sh [--quiet]
set -uo pipefail
cd "$(dirname "$0")"

APP_PORT="8000"
PID_FILE=".server.pid"
QUIET="${1:-}"
killed=0

[ "$QUIET" != "--quiet" ] && echo "Stopping MarkItDown..."

# Primary: the PID we recorded at launch.
if [ -f "$PID_FILE" ]; then
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null && killed=1
  fi
  rm -f "$PID_FILE"
fi

# Fallback: whatever still holds the app port.
for p in $(lsof -nP -iTCP:"${APP_PORT}" -sTCP:LISTEN -t 2>/dev/null || true); do
  kill "$p" 2>/dev/null && killed=1
done

if [ "$QUIET" != "--quiet" ]; then
  [ "$killed" = "1" ] && echo "[OK] MarkItDown stopped." || echo "[OK] No MarkItDown instance was running."
fi
exit 0
