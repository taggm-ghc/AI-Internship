#!/usr/bin/env bash
# Start the Week 1 v2 FastAPI service locally with autoreload.
#
# Usage:
#   ./run.sh            # try port 8000, then 8001, 8002, ... until one's free
#   ./run.sh 8000       # same, but starting from a different base port
#   PORT=8080 ./run.sh  # or via env var
#
# Ported 2026-09-13 from ../../ai-eng-bootcamp.vera/run.sh, which only ever
# checked a single port and bailed on conflict — extended same day with the
# increment-and-retry loop below (see p3m3/week1-module1-checklist.md for the
# best-practices research behind this choice over uvicorn's native
# `--port 0`: that trick lets the OS pick a free port, but this script needs
# to know the concrete port *before* launching uvicorn, to write it to
# .faststream-local-url — recovering which port `--port 0` actually bound to
# would mean parsing it back out of a --reload supervisor/child pair, which
# is more fragile than just trying the next number ourselves).

set -euo pipefail

cd "$(dirname "$0")"

BASE_PORT="${1:-${PORT:-8000}}"
HOST="${HOST:-127.0.0.1}"
MAX_PORT_ATTEMPTS=20

# Bounded with `timeout` because a bare /dev/tcp connect attempt can hang for
# a long time (SYN retries) in sandboxed environments where an unused port
# is neither refused nor reachable, instead of failing fast.
port_is_bound() {
    timeout 1 bash -c "exec 3<>\"/dev/tcp/${HOST}/$1\"" 2>/dev/null
}

PORT=""
for (( offset=0; offset<MAX_PORT_ATTEMPTS; offset++ )); do
    candidate=$((BASE_PORT + offset))
    if ! port_is_bound "$candidate"; then
        PORT="$candidate"
        break
    fi
    if curl -sf -m 2 "http://${HOST}:${candidate}/health" >/dev/null 2>&1; then
        # This is *our own* service already running — report it and stop
        # rather than silently starting a second instance on another port.
        echo "The API already appears to be running at http://${HOST}:${candidate}/health"
        exit 1
    fi
    echo "Port ${candidate} on ${HOST} is in use by another process, trying $((candidate + 1))..." >&2
done

if [[ -z "$PORT" ]]; then
    echo "Error: no free port found in ${BASE_PORT}-$((BASE_PORT + MAX_PORT_ATTEMPTS - 1)) on ${HOST}." >&2
    echo "Free one up, e.g.: pkill -f 'uvicorn main:app'" >&2
    exit 1
fi

# Activate the project virtualenv if it exists and isn't already active.
if [[ -z "${VIRTUAL_ENV:-}" && -f .venv/bin/activate ]]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi

if [[ "$PORT" != "$BASE_PORT" ]]; then
    echo "Port ${BASE_PORT} was busy — using ${PORT} instead."
fi
echo "Starting uvicorn on http://${HOST}:${PORT}"
echo "  health check: http://${HOST}:${PORT}/health"
echo "  API docs:     http://${HOST}:${PORT}/docs"

# Record the active address so demo_page.py can default to it instead of a
# hardcoded port that drifts whenever this is started on a non-default one.
echo "http://${HOST}:${PORT}" > .faststream-local-url

exec uvicorn main:app --reload --host "$HOST" --port "$PORT"
