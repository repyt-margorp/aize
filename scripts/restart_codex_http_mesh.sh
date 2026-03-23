#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
DISPATCH_LOG_PATH="${DISPATCH_LOG_PATH:-$ROOT/.temp/restart-debug/restart-dispatch.log}"
RESTART_LOCK_PATH="${RESTART_LOCK_PATH:-$ROOT/.temp/restart-debug/restart.lock}"

mkdir -p "$(dirname "$DISPATCH_LOG_PATH")"
printf '%s dispatch requested\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" >>"$DISPATCH_LOG_PATH"

nohup setsid /bin/bash -lc \
  "cd '$ROOT' && ROOT='$ROOT' AIZE_RUNTIME_ROOT='${AIZE_RUNTIME_ROOT:-$ROOT/.agent-mesh-runtime}' AIZE_HTTP_HOST='${AIZE_HTTP_HOST:-127.0.0.1}' AIZE_HTTP_PORT='${AIZE_HTTP_PORT:-4123}' RESTART_LOCK_PATH='$RESTART_LOCK_PATH' SYNC_RESTART=1 '$ROOT/restart_codex_http_mesh.sh' \"\$@\" >>'$DISPATCH_LOG_PATH' 2>&1" \
  restart-dispatch "$@" >/dev/null 2>&1 </dev/null &

echo $!
