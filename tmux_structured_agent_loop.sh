#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 3 ]; then
  echo "usage: $0 <manifest-json> <self-agent-id> <event-log>" >&2
  exit 1
fi

MANIFEST_PATH="$1"
SELF_AGENT_ID="$2"
EVENT_LOG="$3"
WORKDIR="${WORKDIR:-$(pwd)}"
RUN_DIR="${RUN_DIR:-$WORKDIR/.tmux-agent-mesh}"

mkdir -p "$RUN_DIR"
: > "$EVENT_LOG"

readarray -t SELF_CFG < <(
  python3 - "$MANIFEST_PATH" "$SELF_AGENT_ID" <<'PY'
import json
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text())
self_id = sys.argv[2]

agent = next((a for a in manifest["agents"] if a["agent_id"] == self_id), None)
if agent is None:
    raise SystemExit(f"unknown agent_id: {self_id}")

route = next((r for r in manifest["routes"] if r["sender_id"] == self_id and r["enabled"]), None)
if route is None:
    raise SystemExit(f"no enabled outbound route for: {self_id}")

partner = next((a for a in manifest["agents"] if a["agent_id"] == route["recipient_id"]), None)
if partner is None:
    raise SystemExit(f"unknown partner for: {self_id}")

print(agent["kind"])
print(agent["display_name"])
print(agent["persona"])
print(agent["max_turns"])
print(partner["agent_id"])
print(partner["display_name"])
print(manifest["run_id"])
PY
)

SELF_KIND="${SELF_CFG[0]}"
SELF_NAME="${SELF_CFG[1]}"
SELF_PERSONA="${SELF_CFG[2]}"
MAX_TURNS="${SELF_CFG[3]}"
PARTNER_AGENT_ID="${SELF_CFG[4]}"
PARTNER_NAME="${SELF_CFG[5]}"
RUN_ID="${SELF_CFG[6]}"

json_log() {
  local phase="$1"
  local turn="$2"
  local text="$3"
  python3 - "$EVENT_LOG" "$RUN_ID" "$SELF_AGENT_ID" "$PARTNER_AGENT_ID" "$phase" "$turn" "$text" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
record = {
    "ts": datetime.now(timezone.utc).isoformat(),
    "run_id": sys.argv[2],
    "agent_id": sys.argv[3],
    "partner_agent_id": sys.argv[4],
    "phase": sys.argv[5],
    "turn": int(sys.argv[6]),
    "text": sys.argv[7],
}
with path.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
PY
}

run_turn() {
  local turn="$1"
  local incoming="$2"
  local out_file="$RUN_DIR/${SELF_AGENT_ID}_turn_${turn}.out"
  local prompt

  prompt=$(
    cat <<EOF
You are $SELF_NAME.
$SELF_PERSONA
Stay in character. Keep each reply to one short sentence. Never mention being an AI.
You are talking to $PARTNER_NAME.
This is your reply number $turn of $MAX_TURNS.
$PARTNER_NAME said: $incoming
EOF
  )

  rm -f "$out_file"

  if [ "$SELF_KIND" = "codex" ]; then
    codex exec --dangerously-bypass-approvals-and-sandbox -o "$out_file" "$prompt" >/dev/null
  elif [ "$SELF_KIND" = "claude" ]; then
    claude -p --dangerously-skip-permissions --system-prompt "You are $SELF_NAME. $SELF_PERSONA Stay in character." "$prompt" > "$out_file"
  else
    echo "unsupported kind: $SELF_KIND" >&2
    exit 1
  fi

  python3 - "$out_file" <<'PY'
from pathlib import Path
import sys
print(Path(sys.argv[1]).read_text().strip())
PY
}

echo "[$SELF_AGENT_ID] READY name=$SELF_NAME partner=$PARTNER_AGENT_ID"

turn=0
while IFS= read -r incoming; do
  turn=$((turn + 1))
  json_log "in" "$turn" "$incoming"
  printf '[%s] TURN %s IN: %s\n' "$SELF_AGENT_ID" "$turn" "$incoming"

  reply="$(run_turn "$turn" "$incoming")"
  json_log "out" "$turn" "$reply"
  printf '[%s] TURN %s OUT: %s\n' "$SELF_AGENT_ID" "$turn" "$reply"

  "$WORKDIR/tmux_route_send.sh" "$MANIFEST_PATH" "$SELF_AGENT_ID" "$PARTNER_AGENT_ID" "$reply"

  if [ "$turn" -ge "$MAX_TURNS" ]; then
    json_log "done" "$turn" "completed"
    echo "[$SELF_AGENT_ID] DONE after $turn turns"
    break
  fi
done
