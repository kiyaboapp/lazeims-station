#!/usr/bin/env bash
# LAZEIMS Station — one-click launcher (Linux/macOS).
# Sets up a local .venv, installs pinned deps, runs migrations, starts the
# server on the LAN, and prints the URL. Later runs need no network.
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
VENV=".venv"
PORT="${STATION_PORT:-8080}"

echo "== LAZEIMS Station =="

# 1) locate Python
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "ERROR: Python 3.11+ not found. Install Python and re-run." >&2
  read -r -p "Press Enter to close..." _ || true
  exit 1
fi

# 2) create venv
if [ ! -x "$VENV/bin/python" ]; then
  echo "Creating virtual environment..."
  "$PY" -m venv "$VENV"
fi

# 3) install deps only when the lock marker changed
MARKER="$VENV/.deps_installed"
if [ ! -f "$MARKER" ] || [ pyproject.toml -nt "$MARKER" ]; then
  echo "Installing dependencies..."
  if [ -d "wheelhouse" ]; then
    "$VENV/bin/pip" install --no-index --find-links wheelhouse -e ../lazeims-common -e . >/dev/null
  else
    "$VENV/bin/pip" install -q -e ../lazeims-common -e . >/dev/null
  fi
  touch "$MARKER"
fi

# 4) migrate DB (import happens via the admin UI / import step)
"$VENV/bin/python" -c "from station.config import load_config; from station.db import connect; from station.migrations import apply_migrations; c=connect(load_config().db_path); print('schema v'+str(apply_migrations(c)))"

# 5) detect LAN IPv4
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -z "${IP:-}" ] && IP="127.0.0.1"

echo ""
echo "  Open on this device : http://127.0.0.1:$PORT"
echo "  Open on the LAN     : http://$IP:$PORT"
echo ""

# 6) start server
exec "$VENV/bin/python" -m uvicorn station.main:app --host 0.0.0.0 --port "$PORT"
