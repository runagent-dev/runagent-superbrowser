#!/usr/bin/env bash
# All-in-one container entrypoint: register the agent, start the TS browser
# engine in the background, wait for it to be healthy, then run the RunAgent
# local agent server in the foreground. Supervises both — if either exits, the
# container goes down (so we never serve a dead engine). tini is PID 1.
set -uo pipefail

ENGINE_URL="${SUPERBROWSER_URL:-http://127.0.0.1:3100}"
AGENT_DIR="/app/deploy"
SERVE_HOST="${RUNAGENT_HOST:-0.0.0.0}"
SERVE_PORT="${RUNAGENT_PORT:-8450}"

log() { echo "[entrypoint] $*" >&2; }

ENGINE_PID=""
SERVE_PID=""
shutdown() {
  log "shutting down"
  [ -n "$SERVE_PID" ] && kill -TERM "$SERVE_PID" 2>/dev/null || true
  [ -n "$ENGINE_PID" ] && kill -TERM "$ENGINE_PID" 2>/dev/null || true
}
trap shutdown TERM INT

# 1. Register the all-zeros agent in the local DB so `runagent serve` passes its
#    agent-id validation. Idempotent, no network, no API key. Feed /dev/null so
#    the "update existing agent?" prompt (fires when already registered) can't
#    block a detached container — it just takes the default (no) and continues.
log "registering agent at ${AGENT_DIR}"
runagent config --register-agent "${AGENT_DIR}" </dev/null \
  || log "register returned non-zero (likely already registered) — continuing"

# 2. Start the TS browser engine in the background (http mode needs no LLM key).
log "starting TS engine: node build/index.js http (PORT=${PORT:-3100})"
node /app/build/index.js http &
ENGINE_PID=$!

# 3. Wait for the engine health endpoint; bail early if the engine dies.
log "waiting for engine at ${ENGINE_URL}/health"
for _ in $(seq 1 90); do
  if curl -fsS "${ENGINE_URL}/health" >/dev/null 2>&1; then
    log "engine healthy"
    break
  fi
  if ! kill -0 "$ENGINE_PID" 2>/dev/null; then
    log "engine process exited during startup"
    exit 1
  fi
  sleep 1
done

# 4. Start the RunAgent local agent server (serves deploy/main.py:run on :8450).
log "starting runagent serve on ${SERVE_HOST}:${SERVE_PORT}"
runagent serve "${AGENT_DIR}" --host "${SERVE_HOST}" --port "${SERVE_PORT}" --no-animation &
SERVE_PID=$!

# 5. Wait on whichever child exits first; propagate its exit code.
wait -n "$ENGINE_PID" "$SERVE_PID"
code=$?
log "a supervised process exited (code ${code}); taking the container down"
shutdown
exit "$code"
