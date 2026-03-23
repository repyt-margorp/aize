#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$SCRIPT_DIR"
RUNTIME_ROOT="$ROOT/.agent-mesh-runtime"
HTTP_HOST="0.0.0.0"
HTTP_PORT="4123"
LOG_PATH="$ROOT/.temp/restart-debug/launcher.log"
SUPERVISOR_LOG_PATH="$ROOT/.temp/restart-debug/restart-supervisor.log"
RESTART_LOCK_PATH="$ROOT/.temp/restart-debug/restart.lock"
PYTHON="${PYTHON:-/usr/bin/python3}"
HEALTH_URL="https://$HTTP_HOST:$HTTP_PORT/health"
START_TIMEOUT_SECONDS="20"

parent_pattern="cli.run_codex_http_mesh --runtime-root $RUNTIME_ROOT"
router_pattern="python3 -m kernel.router --manifest $RUNTIME_ROOT/manifest.json"
adapter_pattern="python3 -m runtime.cli_service_adapter --manifest $RUNTIME_ROOT/manifest.json"

kill_matching_groups() {
  local signal_name="$1"
  local pids="$2"
  local groups
  groups="$(
    for pid in $pids; do
      ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' '
    done | awk 'NF' | sort -u
  )"
  if [[ -z "$groups" ]]; then
    return 0
  fi
  for group in $groups; do
    kill "-$signal_name" -- "-$group" 2>/dev/null || true
  done
}

terminate_matches() {
  local pattern="$1"
  local pids
  pids="$(pgrep -f "$pattern" || true)"
  if [[ -z "$pids" ]]; then
    return 0
  fi

  kill_matching_groups TERM "$pids"
  for _ in $(seq 1 20); do
    if ! pgrep -f "$pattern" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.5
  done

  pids="$(pgrep -f "$pattern" || true)"
  if [[ -n "$pids" ]]; then
    kill_matching_groups KILL "$pids"
  fi
}

log() {
  printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*" >>"$SUPERVISOR_LOG_PATH"
}

mkdir -p "$(dirname "$LOG_PATH")"
mkdir -p "$(dirname "$SUPERVISOR_LOG_PATH")"

if [[ "${SYNC_RESTART:-0}" != "1" && "${DETACHED_RESTART:-0}" != "1" ]]; then
  : >"$SUPERVISOR_LOG_PATH"
  if command -v setsid >/dev/null 2>&1; then
    nohup setsid env \
      ROOT="$ROOT" \
      AIZE_RUNTIME_ROOT="$RUNTIME_ROOT" \
      AIZE_HTTP_HOST="$HTTP_HOST" \
      AIZE_HTTP_PORT="$HTTP_PORT" \
      LOG_PATH="$LOG_PATH" \
      SUPERVISOR_LOG_PATH="$SUPERVISOR_LOG_PATH" \
      RESTART_LOCK_PATH="$RESTART_LOCK_PATH" \
      PYTHON="$PYTHON" \
      HEALTH_URL="$HEALTH_URL" \
      START_TIMEOUT_SECONDS="$START_TIMEOUT_SECONDS" \
      DETACHED_RESTART=1 \
      "$0" >/dev/null 2>&1 </dev/null &
  else
    nohup env \
      ROOT="$ROOT" \
      AIZE_RUNTIME_ROOT="$RUNTIME_ROOT" \
      AIZE_HTTP_HOST="$HTTP_HOST" \
      AIZE_HTTP_PORT="$HTTP_PORT" \
      LOG_PATH="$LOG_PATH" \
      SUPERVISOR_LOG_PATH="$SUPERVISOR_LOG_PATH" \
      RESTART_LOCK_PATH="$RESTART_LOCK_PATH" \
      PYTHON="$PYTHON" \
      HEALTH_URL="$HEALTH_URL" \
      START_TIMEOUT_SECONDS="$START_TIMEOUT_SECONDS" \
      DETACHED_RESTART=1 \
      "$0" >/dev/null 2>&1 </dev/null &
  fi
  echo $!
  exit 0
fi

mkdir -p "$(dirname "$RESTART_LOCK_PATH")"
exec 9>"$RESTART_LOCK_PATH"
if ! flock -n 9; then
  printf '%s restart already in progress\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" >>"$SUPERVISOR_LOG_PATH"
  exit 0
fi

log "restart supervisor begin pid=$$ detached=${DETACHED_RESTART:-0} sync=${SYNC_RESTART:-0}"
terminate_matches "$parent_pattern"
log "parent processes terminated"
terminate_matches "$router_pattern"
log "router processes terminated"
terminate_matches "$adapter_pattern"
log "adapter processes terminated"

cd "$ROOT"
if command -v setsid >/dev/null 2>&1; then
  nohup setsid /bin/bash -lc "exec 9>&-; cd '$ROOT' && env -i PATH='$PATH' HOME='$HOME' PYTHONPATH='$ROOT/src' AIZE_ROOT='$ROOT' AIZE_RUNTIME_ROOT='$RUNTIME_ROOT' AIZE_HTTP_HOST='$HTTP_HOST' AIZE_HTTP_PORT='$HTTP_PORT' '$PYTHON' -m cli.run_codex_http_mesh --runtime-root '$RUNTIME_ROOT'" >"$LOG_PATH" 2>&1 </dev/null &
else
  nohup /bin/bash -lc "exec 9>&-; cd '$ROOT' && env -i PATH='$PATH' HOME='$HOME' PYTHONPATH='$ROOT/src' AIZE_ROOT='$ROOT' AIZE_RUNTIME_ROOT='$RUNTIME_ROOT' AIZE_HTTP_HOST='$HTTP_HOST' AIZE_HTTP_PORT='$HTTP_PORT' '$PYTHON' -m cli.run_codex_http_mesh --runtime-root '$RUNTIME_ROOT'" >"$LOG_PATH" 2>&1 </dev/null &
fi
new_pid=$!
log "launched new parent pid=$new_pid"

for _ in $(seq 1 "$START_TIMEOUT_SECONDS"); do
  if ! kill -0 "$new_pid" 2>/dev/null; then
    log "new parent exited before health check completed"
    tail -n 80 "$LOG_PATH" >&2 || true
    exit 1
  fi
  if curl -fsSk "$HEALTH_URL" >/dev/null 2>&1; then
    log "health check recovered for pid=$new_pid"
    echo "$new_pid"
    exit 0
  fi
  log "waiting for health pid=$new_pid"
  sleep 1
done

log "health check timed out for pid=$new_pid"
tail -n 80 "$LOG_PATH" >&2 || true
exit 1
