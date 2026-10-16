#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
DISPATCH_LOG_PATH="${DISPATCH_LOG_PATH:-$ROOT/.temp/restart-debug/restart-dispatch.log}"
RESTART_LOCK_PATH="${RESTART_LOCK_PATH:-$ROOT/.temp/restart-debug/restart.lock}"
RUNTIME_ROOT="${AIZE_RUNTIME_ROOT:-$ROOT/.aize-runtime}"
LEGACY_RUNTIME_ROOT="$ROOT/.agent""-mesh-runtime"
PYTHON="${PYTHON:-/usr/bin/python3}"
AIZE_HTTP_HOST_VALUE="${AIZE_HTTP_HOST:-0.0.0.0}"

if [[ -n "${AIZE_HTTP_PORT:-}" ]]; then
  AIZE_HTTP_PORT_VALUE="$AIZE_HTTP_PORT"
else
  AIZE_HTTP_PORT_VALUE="$(
    "$PYTHON" - "$RUNTIME_ROOT" "$LEGACY_RUNTIME_ROOT" <<'PY' 2>/dev/null || true
import json
import sys
from pathlib import Path

for runtime_root in [Path(arg) for arg in sys.argv[1:]]:
    for path in (
        runtime_root / "state" / "services.json",
        runtime_root / "manifest.json",
    ):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
        services = data.get("services", {})
        if isinstance(services, dict):
            service = services.get("service-http-001")
        elif isinstance(services, list):
            service = next(
                (
                    item
                    for item in services
                    if isinstance(item, dict)
                    and item.get("service_id") == "service-http-001"
                ),
                None,
            )
        else:
            service = None
        if not isinstance(service, dict):
            continue
        port = (service.get("config") or {}).get("port")
        if isinstance(port, int) or (isinstance(port, str) and port.isdigit()):
            print(port)
            raise SystemExit(0)
PY
  )"
  AIZE_HTTP_PORT_VALUE="${AIZE_HTTP_PORT_VALUE:-4123}"
fi

mkdir -p "$(dirname "$DISPATCH_LOG_PATH")"
printf '%s dispatch requested\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" >>"$DISPATCH_LOG_PATH"

nohup setsid /bin/bash -lc \
  "cd '$ROOT' && ROOT='$ROOT' AIZE_RUNTIME_ROOT='$RUNTIME_ROOT' AIZE_HTTP_HOST='$AIZE_HTTP_HOST_VALUE' AIZE_HTTP_PORT='$AIZE_HTTP_PORT_VALUE' PYTHON='$PYTHON' RESTART_LOCK_PATH='$RESTART_LOCK_PATH' SYNC_RESTART=1 '$ROOT/restart_aize_unit.sh' \"\$@\" >>'$DISPATCH_LOG_PATH' 2>&1" \
  restart-dispatch "$@" >/dev/null 2>&1 </dev/null &

echo $!
