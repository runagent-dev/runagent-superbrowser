#!/usr/bin/env bash
# All-in-one container entrypoint: register the agent, start the TS browser
# engine in the background, wait for it to be healthy, then run the RunAgent
# local agent server in the foreground. Supervises both — a dead child is
# restarted IN PLACE (so a single blip doesn't wipe a healthy engine + its T3
# profiles mid-run); the container is only taken down if a child crash-loops
# (>=3 restarts within a 60s rolling window) or the engine can't become healthy.
# tini is PID 1.
set -uo pipefail

ENGINE_URL="${SUPERBROWSER_URL:-http://127.0.0.1:3100}"
AGENT_DIR="/app/deploy"
SERVE_HOST="${RUNAGENT_HOST:-0.0.0.0}"
SERVE_PORT="${RUNAGENT_PORT:-8450}"
CRASH_LOOP_MAX="${SUPERVISOR_CRASH_LOOP_MAX:-3}"
CRASH_LOOP_WINDOW="${SUPERVISOR_CRASH_LOOP_WINDOW:-60}"

# Optional chat gateway (WhatsApp/Telegram/Discord + web console). Default OFF —
# when unset, every line below behaves exactly as before. Enable with
# GATEWAY_ENABLED=1 (and publish 127.0.0.1:8460 in compose). The gateway posts
# human-handoff links to the engine's HANDOFF_WEBHOOK_URL, so default it to the
# gateway's own sink when enabled and not already set.
GATEWAY_ENABLED="${GATEWAY_ENABLED:-0}"
GATEWAY_PORT="${SB_GATEWAY_CONSOLE_PORT:-8460}"
if [ "$GATEWAY_ENABLED" = "1" ] && [ -z "${HANDOFF_WEBHOOK_URL:-}" ]; then
  export HANDOFF_WEBHOOK_URL="http://127.0.0.1:${GATEWAY_PORT}/api/hooks/handoff"
fi

log() { echo "[entrypoint] $*" >&2; }

ENGINE_PID=""
SERVE_PID=""
GATEWAY_PID=""
shutdown() {
  log "shutting down"
  [ -n "$GATEWAY_PID" ] && kill -TERM "$GATEWAY_PID" 2>/dev/null || true
  [ -n "$SERVE_PID" ] && kill -TERM "$SERVE_PID" 2>/dev/null || true
  [ -n "$ENGINE_PID" ] && kill -TERM "$ENGINE_PID" 2>/dev/null || true
}
trap 'shutdown; exit 143' TERM INT

start_engine() {
  log "starting TS engine: node build/index.js http (PORT=${PORT:-3100})"
  node /app/build/index.js http &
  ENGINE_PID=$!
}

# Wait for the engine health endpoint; bail if the engine dies during the wait.
wait_for_engine() {
  log "waiting for engine at ${ENGINE_URL}/health"
  for _ in $(seq 1 90); do
    if curl -fsS "${ENGINE_URL}/health" >/dev/null 2>&1; then
      log "engine healthy"
      return 0
    fi
    if ! kill -0 "$ENGINE_PID" 2>/dev/null; then
      log "engine process exited during startup"
      return 1
    fi
    sleep 1
  done
  log "engine health timed out"
  return 1
}

start_serve() {
  log "starting runagent serve on ${SERVE_HOST}:${SERVE_PORT}"
  runagent serve "${AGENT_DIR}" --host "${SERVE_HOST}" --port "${SERVE_PORT}" --no-animation &
  SERVE_PID=$!
}

start_gateway() {
  log "starting chat gateway on :${GATEWAY_PORT} (GATEWAY_ENABLED=1)"
  superbrowser-gateway --console-port "${GATEWAY_PORT}" --bind "${SB_GATEWAY_BIND:-127.0.0.1}" &
  GATEWAY_PID=$!
}

# 1. Register the all-zeros agent in the local DB so `runagent serve` passes its
#    agent-id validation. Idempotent, no network, no API key. Feed /dev/null so
#    the "update existing agent?" prompt (fires when already registered) can't
#    block a detached container — it just takes the default (no) and continues.
log "registering agent at ${AGENT_DIR}"
runagent config --register-agent "${AGENT_DIR}" </dev/null \
  || log "register returned non-zero (likely already registered) — continuing"

# 2. Boot the TS engine (http mode needs no LLM key), gate on its health, then
#    boot the RunAgent local agent server (serves deploy/main.py:run on :8450).
start_engine
if ! wait_for_engine; then
  log "engine failed to become healthy on boot — taking the container down"
  shutdown
  exit 1
fi
start_serve

# 2b. Optionally boot the chat gateway (default off). It is auxiliary infra: a
#     gateway crash never takes the container down — the supervisor just
#     restarts it in place so the engine + serve keep running.
if [ "$GATEWAY_ENABLED" = "1" ]; then
  if command -v superbrowser-gateway >/dev/null 2>&1; then
    start_gateway
  else
    log "GATEWAY_ENABLED=1 but superbrowser-gateway is not installed (build the image with the gateway extra) — skipping"
    GATEWAY_ENABLED=0
  fi
fi

# 3. Supervisor loop: poll both children, restart a dead one in place, and only
#    give up (take the container down) when a child crash-loops. Rationale: the
#    in-memory TS session Map lives only in the engine process, and T3 Chrome
#    profiles are held by the serve process — nuking the whole container on any
#    single exit (the old `wait -n` behavior) turned a transient renderer crash
#    into a full session wipe + 404 for the in-flight sb.run(). Restarting the
#    dead child alone preserves the healthy one.
engine_restarts=0
serve_restarts=0
engine_window_start=$SECONDS
serve_window_start=$SECONDS

while true; do
  sleep 2

  if ! kill -0 "$ENGINE_PID" 2>/dev/null; then
    now=$SECONDS
    if [ $((now - engine_window_start)) -gt "$CRASH_LOOP_WINDOW" ]; then
      engine_restarts=0
      engine_window_start=$now
    fi
    engine_restarts=$((engine_restarts + 1))
    if [ "$engine_restarts" -gt "$CRASH_LOOP_MAX" ]; then
      log "engine crash-looped ${engine_restarts}x within ${CRASH_LOOP_WINDOW}s — taking the container down"
      shutdown
      exit 1
    fi
    # Reap the dead child (its parent is this script, not tini, so tini won't
    # collect the zombie until we wait on it).
    wait "$ENGINE_PID" 2>/dev/null || true
    log "engine died — restarting in place (${engine_restarts}/${CRASH_LOOP_MAX} in window)"
    start_engine
    if ! wait_for_engine; then
      log "engine failed health after restart — taking the container down"
      shutdown
      exit 1
    fi
  fi

  if ! kill -0 "$SERVE_PID" 2>/dev/null; then
    now=$SECONDS
    if [ $((now - serve_window_start)) -gt "$CRASH_LOOP_WINDOW" ]; then
      serve_restarts=0
      serve_window_start=$now
    fi
    serve_restarts=$((serve_restarts + 1))
    if [ "$serve_restarts" -gt "$CRASH_LOOP_MAX" ]; then
      log "runagent serve crash-looped ${serve_restarts}x within ${CRASH_LOOP_WINDOW}s — taking the container down"
      shutdown
      exit 1
    fi
    # Reap the dead child before restarting (see engine branch).
    wait "$SERVE_PID" 2>/dev/null || true
    log "runagent serve died — restarting in place (${serve_restarts}/${CRASH_LOOP_MAX} in window)"
    start_serve
  fi

  # Gateway (optional): restart in place on exit; never crash-loop the
  # container (it's not a core service). Backoff is implicit via the 2s poll.
  if [ "$GATEWAY_ENABLED" = "1" ] && [ -n "$GATEWAY_PID" ] && ! kill -0 "$GATEWAY_PID" 2>/dev/null; then
    wait "$GATEWAY_PID" 2>/dev/null || true
    log "chat gateway died — restarting in place"
    start_gateway
  fi
done
