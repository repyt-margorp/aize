#!/usr/bin/env bash
# register_user.sh — Securely register a new user in the Aize HTTP mesh.
#
# Usage: ./scripts/register_user.sh [new-username]
#
# Admin (superuser) credentials and the new user's password are always
# prompted interactively via read -s — they are never passed as command-line
# arguments, preventing exposure in process listings and shell history.
#
# The script requires a running mesh at AIZE_HTTP_HOST:AIZE_HTTP_PORT.
# The admin account must have the "superuser" role.

set -euo pipefail

HOST="${AIZE_HTTP_HOST:-127.0.0.1}"
PORT="${AIZE_HTTP_PORT:-4123}"
BASE_URL="http://${HOST}:${PORT}"

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
LOGIN_RESPONSE=$(curl -sf -X POST "${BASE_URL}/login" \
    -H 'Content-Type: application/json' \
    -c "$COOKIE_JAR" \
    --data-binary "@${LOGIN_JSON}") || {
    echo "Error: admin login failed (check credentials or that the mesh is running at ${BASE_URL})." >&2
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
REG_RESPONSE=$(curl -sf -X POST "${BASE_URL}/register" \
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
