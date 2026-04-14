#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="$SCRIPT_DIR/server.log"
PID_FILE="$SCRIPT_DIR/server.pid"
CMD="python3 -m uvicorn main:app --host 0.0.0.0 --port 8000 --log-level warning"

_find_pid() {
    pgrep -f "uvicorn main:app" 2>/dev/null | head -1 || true
}

do_start() {
    local pid
    pid=$(_find_pid)
    if [[ -n "$pid" ]]; then
        echo "Already running (pid $pid)"
        return
    fi
    cd "$SCRIPT_DIR"
    nohup $CMD >> "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    echo "Started (pid $!)"
}

do_stop() {
    local pid
    pid=$(_find_pid)
    if [[ -z "$pid" ]]; then
        echo "Not running"
        return
    fi
    kill "$pid"
    echo "Stopped (pid $pid)"
    rm -f "$PID_FILE"
}

do_status() {
    local pid
    pid=$(_find_pid)
    if [[ -n "$pid" ]]; then
        echo "Running (pid $pid)"
    else
        echo "Not running"
    fi
}

case "${1:-}" in
    start)   do_start ;;
    stop)    do_stop ;;
    restart) do_stop; sleep 1; do_start ;;
    status)  do_status ;;
    log)     tail -f "$LOG_FILE" ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|log}"
        exit 1
        ;;
esac
