#!/usr/bin/env bash
# register_user.sh — Securely register a new user in the AIze Unit runtime.
#
# Usage: ./scripts/register_user.sh [new-username]
#
# Admin (superuser) credentials and the new user's password are always
# prompted interactively via read -s — they are never passed as command-line
# arguments, preventing exposure in process listings and shell history.
#
# The script resolves the active HttpBridge host/port from runtime state unless
# AIZE_HTTP_HOST / AIZE_HTTP_PORT override it explicitly, and it follows
# AIZE_TLS to choose HTTPS vs HTTP for the local bridge.
# The admin account must have the "superuser" role.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
RUNTIME_ROOT="${AIZE_RUNTIME_ROOT:-$ROOT/.aize-runtime}"

resolve_runtime_http() {
    python3 - "$RUNTIME_ROOT" <<'PY'
import json
import os
import sys
from pathlib import Path

runtime_root = Path(sys.argv[1])
host = str(os.environ.get("AIZE_HTTP_HOST") or "127.0.0.1").strip() or "127.0.0.1"
if host in {"0.0.0.0", "::"}:
    host = "127.0.0.1"
scheme = "http" if str(os.environ.get("AIZE_TLS", "true")).strip().lower() in {"0", "false", "no", "off"} else "https"

configured_port = str(os.environ.get("AIZE_HTTP_PORT") or "").strip()
if configured_port:
    port = configured_port
else:
    port = "4123"
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
                    and str(item.get("service_id") or item.get("id") or "").strip() == "service-http-001"
                ),
                None,
            )
        else:
            service = None
        candidate = str(((service or {}).get("config") or {}).get("port") or "").strip() if isinstance(service, dict) else ""
        if candidate:
            port = candidate
            break

print(host)
print(port)
print(scheme)
PY
}

mapfile -t _runtime_http < <(resolve_runtime_http)
HOST="${_runtime_http[0]:-127.0.0.1}"
PORT="${_runtime_http[1]:-4123}"
SCHEME="${_runtime_http[2]:-https}"
BASE_URL="${SCHEME}://${HOST}:${PORT}"
CURL_TLS_ARGS=()
if [[ "$SCHEME" == "https" ]]; then
    CURL_TLS_ARGS=(-k)
fi

# Temp files — always cleaned up on exit
COOKIE_JAR=""
LOGIN_JSON=""
REG_JSON=""

cleanup() {
    [[ -n "$COOKIE_JAR" ]] && rm -f "$COOKIE_JAR"
    [[ -n "$LOGIN_JSON" ]] && rm -f "$LOGIN_JSON"
    [[ -n "$REG_JSON" ]]   && rm -f "$REG_JSON"
}
trap cleanup EXIT

COOKIE_JAR="$(mktemp)"
LOGIN_JSON="$(mktemp)"
REG_JSON="$(mktemp)"

# --- Collect inputs ---

if [[ $# -ge 1 ]]; then
    NEW_USERNAME="$1"
else
    printf 'New username: '
    read -r NEW_USERNAME
fi

printf '\nAdmin (superuser) credentials required to create users.\n'
printf 'Admin username: '
read -r ADMIN_USERNAME

printf 'Admin password: '
read -rs ADMIN_PASSWORD
echo

printf '\nNew user password (8+ chars): '
read -rs NEW_PASSWORD
echo

printf 'Confirm new user password: '
read -rs NEW_PASSWORD_CONFIRM
echo

if [[ "$NEW_PASSWORD" != "$NEW_PASSWORD_CONFIRM" ]]; then
    echo "Error: passwords do not match." >&2
    exit 1
fi

if [[ ${#NEW_PASSWORD} -lt 8 ]]; then
    echo "Error: new user password must be at least 8 characters." >&2
    exit 1
fi

# --- Step 1: Login as admin ---
# Credentials are piped via stdin to python3 so they never appear in ps output.

printf '%s\n%s\n' "$ADMIN_USERNAME" "$ADMIN_PASSWORD" | \
    python3 -c "
import json, sys
lines = sys.stdin.read().splitlines()
sys.stdout.write(json.dumps({'username': lines[0], 'password': lines[1]}))
" > "$LOGIN_JSON"

printf 'Authenticating admin...\n'
LOGIN_RESPONSE=$(curl -sf "${CURL_TLS_ARGS[@]}" -X POST "${BASE_URL}/login" \
    -H 'Content-Type: application/json' \
    -c "$COOKIE_JAR" \
    --data-binary "@${LOGIN_JSON}") || {
    echo "Error: admin login failed (check credentials or that the Unit runtime is running at ${BASE_URL})." >&2
    exit 1
}

LOGIN_OK=$(python3 -c "
import json, sys
d = json.loads(sys.stdin.read())
print('yes' if d.get('ok') else 'no')
" <<< "$LOGIN_RESPONSE" 2>/dev/null || echo "no")

if [[ "$LOGIN_OK" != "yes" ]]; then
    echo "Error: admin login was rejected." >&2
    exit 1
fi

# --- Step 2: Register the new user ---
# Again, credentials flow through stdin only.

printf '%s\n%s\n' "$NEW_USERNAME" "$NEW_PASSWORD" | \
    python3 -c "
import json, sys
lines = sys.stdin.read().splitlines()
sys.stdout.write(json.dumps({'username': lines[0], 'password': lines[1]}))
" > "$REG_JSON"

printf 'Registering user "%s"...\n' "$NEW_USERNAME"
REG_RESPONSE=$(curl -sf "${CURL_TLS_ARGS[@]}" -X POST "${BASE_URL}/register" \
    -H 'Content-Type: application/json' \
    -b "$COOKIE_JAR" \
    --data-binary "@${REG_JSON}") || {
    echo "Error: user registration request failed." >&2
    exit 1
}

python3 -c "
import json, sys
d = json.loads(sys.stdin.read())
if d.get('ok'):
    print(\"Created user '{}' with role '{}'\".format(d['username'], d['role']))
else:
    print('Error: {}'.format(d.get('error', 'unknown')), file=sys.stderr)
    sys.exit(1)
" <<< "$REG_RESPONSE"
