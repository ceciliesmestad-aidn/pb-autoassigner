#!/usr/bin/env bash
# PB_assignerV2 launch script.
# Usage:
#   ./launch.sh          start backend + frontend (dev mode)
#   ./launch.sh backend  backend only
#   ./launch.sh setup    first-time setup only, then exit

set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

VENV="$ROOT/.venv"
BACKEND_PORT=8765
FRONTEND_PORT=5173
BACKEND_PID=""
FRONTEND_PID=""

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
RESET='\033[0m'

info()  { echo -e "${GREEN}>>>${RESET} $*"; }
warn()  { echo -e "${YELLOW}!!! $*${RESET}"; }
error() { echo -e "${RED}ERR $*${RESET}"; exit 1; }

cleanup() {
  echo ""
  info "shutting down..."
  [ -n "$BACKEND_PID" ]  && kill "$BACKEND_PID"  2>/dev/null || true
  [ -n "$FRONTEND_PID" ] && kill "$FRONTEND_PID" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup INT TERM

setup() {
  # Load .env if present
  if [ -f "$ROOT/.env" ]; then
    set -o allexport
    source "$ROOT/.env"
    set +o allexport
  fi

  # Check tokens
  if [ -z "${PB_TOKEN:-}" ] || [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    echo ""
    warn "Fill in $ROOT/.env and re-run:"
    echo "  PB_TOKEN=pb_live_..."
    echo "  ANTHROPIC_API_KEY=sk-ant-..."
    echo ""
    exit 0
  fi

  # Python venv
  info "checking venv..."
  if [ ! -f "$VENV/bin/python" ]; then
    python3 -m venv "$VENV"
    info "created venv"
  fi

  # Python deps
  info "checking Python deps..."
  if ! "$VENV/bin/python" -c "import fastapi, anthropic, uvicorn" 2>/dev/null; then
    "$VENV/bin/pip" install -q --upgrade pip
    "$VENV/bin/pip" install -q -e '.[dev]'
    info "installed backend deps"
  fi

  # config.toml (tokens come from .env, not here)
  if [ ! -f "$ROOT/config.toml" ]; then
    cp "$ROOT/config.example.toml" "$ROOT/config.toml"
    info "created config.toml"
  fi

  # Data dir + DB
  mkdir -p "$ROOT/data"
  info "initialising database..."
  "$VENV/bin/python" -m backend.cli init-db

  # Frontend deps
  info "checking frontend deps..."
  if [ ! -d "$ROOT/frontend/node_modules" ]; then
    (cd "$ROOT/frontend" && npm install --silent)
    info "installed frontend deps"
  fi
}

CMD="${1:-all}"
setup

if [ "$CMD" = "setup" ]; then
  info "setup complete."
  exit 0
fi

# Start backend
info "starting backend on :$BACKEND_PORT..."
"$VENV/bin/python" -m backend.cli serve \
  --host 127.0.0.1 --port "$BACKEND_PORT" --reload \
  > "$ROOT/data/backend.log" 2>&1 &
BACKEND_PID=$!

# Wait for backend to be ready
for i in $(seq 1 20); do
  if curl -sf "http://127.0.0.1:$BACKEND_PORT/api/health" >/dev/null 2>&1; then
    info "backend ready"
    break
  fi
  sleep 0.4
  if [ "$i" -eq 20 ]; then
    error "backend didn't start. Check: tail $ROOT/data/backend.log"
  fi
done

if [ "$CMD" = "backend" ]; then
  info "backend only — Ctrl+C to stop"
  tail -f "$ROOT/data/backend.log" &
  wait "$BACKEND_PID"
  exit 0
fi

# Start frontend
info "starting frontend on :$FRONTEND_PORT..."
(cd "$ROOT/frontend" && npm run dev --silent) \
  > "$ROOT/data/frontend.log" 2>&1 &
FRONTEND_PID=$!

sleep 1
if ! kill -0 "$FRONTEND_PID" 2>/dev/null; then
  error "frontend failed. Check: tail $ROOT/data/frontend.log"
fi

# Open browser
sleep 0.5
open "http://127.0.0.1:$FRONTEND_PORT" 2>/dev/null || true

echo ""
echo -e "${GREEN}==========================================${RESET}"
echo -e "  App:   http://127.0.0.1:$FRONTEND_PORT"
echo -e "  API:   http://127.0.0.1:$BACKEND_PORT/docs"
echo -e "  Logs:  tail -f data/backend.log"
echo -e "${GREEN}==========================================${RESET}"
echo ""
echo "  Ctrl+C to stop"
echo ""

wait
