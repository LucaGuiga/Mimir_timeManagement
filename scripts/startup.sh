#!/usr/bin/env bash
# Starts Athena: launches core/main.py, which supervises the poller and the GUI.
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
PY="${PYTHON:-python3}"
mkdir -p run logs
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> logs/startup.log; }

# 1. MySQL must be reachable.
if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files 2>/dev/null | grep -qE '^(mysql|mariadb)\.service'; then
  if ! systemctl is-active --quiet mysql && ! systemctl is-active --quiet mariadb; then
    echo "startup: mysql/mariadb service is not active" >&2; log "refused: mysql not active"; exit 1
  fi
elif ! "$PY" -m db.db --check >/dev/null 2>&1; then
  echo "startup: db/db.py --check failed, is MySQL running and config/config.yaml filled?" >&2; log "refused: db check failed"; exit 1
fi

# 2. Config must be complete.
if ! "$PY" -c "from core.config import load_config, validate_config; validate_config(load_config())"; then
  echo "startup: config/config.yaml is missing required values" >&2; log "refused: config invalid"; exit 1
fi

# 3. Refuse to double start.
if [ -f run/main.pid ] && kill -0 "$(cat run/main.pid)" 2>/dev/null; then
  echo "startup: main already running with pid $(cat run/main.pid)" >&2; log "refused: main already running pid $(cat run/main.pid)"; exit 1
fi
rm -f run/main.pid run/poller.pid run/gui.pid

# 4. Launch main; it starts the poller and GUI itself.
nohup "$PY" -m core.main >> logs/main.out 2>&1 &
MAIN_PID=$!
log "started main pid $MAIN_PID"
echo "athena main started, pid $MAIN_PID"

# 5. Record the children once main has had time to launch them.
(
  sleep 5
  P="$(cat run/poller.pid 2>/dev/null || echo none)"; G="$(cat run/gui.pid 2>/dev/null || echo none)"
  log "children: poller pid $P, gui pid $G"
) &
exit 0
