#!/usr/bin/env bash
# Stops Athena: SIGTERM to main, which cascades to the poller and GUI and waits for their cycles.
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
mkdir -p run logs
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> logs/startup.log; }
GRACE=45

if [ ! -f run/main.pid ]; then
  echo "shutdown: no run/main.pid, nothing to stop"; log "shutdown: no main.pid"
  rm -f run/*.pid; exit 0
fi
MAIN_PID="$(cat run/main.pid)"
if ! kill -0 "$MAIN_PID" 2>/dev/null; then
  echo "shutdown: main pid $MAIN_PID is not running, cleaning pidfiles"; log "shutdown: stale main.pid $MAIN_PID"
  rm -f run/*.pid; exit 0
fi

kill -TERM "$MAIN_PID" 2>/dev/null
log "shutdown: SIGTERM sent to main pid $MAIN_PID"
for _ in $(seq 1 "$GRACE"); do
  kill -0 "$MAIN_PID" 2>/dev/null || break
  sleep 1
done

if kill -0 "$MAIN_PID" 2>/dev/null; then
  echo "shutdown: main did not exit in ${GRACE}s, sending SIGKILL" >&2
  kill -KILL "$MAIN_PID" 2>/dev/null
  for f in run/*.pid; do
    [ -f "$f" ] || continue
    P="$(cat "$f")"
    kill -0 "$P" 2>/dev/null && kill -KILL "$P" 2>/dev/null && log "shutdown: SIGKILL $(basename "$f" .pid) pid $P"
  done
  log "shutdown: forced after ${GRACE}s"
else
  log "shutdown: main exited cleanly"
fi
rm -f run/*.pid
echo "athena stopped"
exit 0
