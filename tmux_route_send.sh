#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 4 ]; then
  echo "usage: $0 <manifest-json> <sender-agent-id> <recipient-agent-id> <payload>" >&2
  exit 1
fi

MANIFEST_PATH="$1"
SENDER_ID="$2"
RECIPIENT_ID="$3"
PAYLOAD="$4"

export MANIFEST_PATH SENDER_ID RECIPIENT_ID PAYLOAD

python3 <<'PY'
import json
import os
import subprocess
import sys
from pathlib import Path

manifest = json.loads(Path(os.environ["MANIFEST_PATH"]).read_text())
sender_id = os.environ["SENDER_ID"]
recipient_id = os.environ["RECIPIENT_ID"]
payload = os.environ["PAYLOAD"]

allowed = False
for route in manifest["routes"]:
    if route["sender_id"] == sender_id and route["recipient_id"] == recipient_id and route["enabled"]:
        allowed = True
        break

if not allowed:
    print(f"route denied: {sender_id} -> {recipient_id}", file=sys.stderr)
    sys.exit(2)

recipient = None
for agent in manifest["agents"]:
    if agent["agent_id"] == recipient_id:
        recipient = agent
        break

if recipient is None:
    print(f"unknown recipient: {recipient_id}", file=sys.stderr)
    sys.exit(3)

pane_id = recipient["pane_id"]
subprocess.run(["tmux", "send-keys", "-t", pane_id, "-l", payload], check=True)
subprocess.run(["tmux", "send-keys", "-t", pane_id, "Enter"], check=True)
PY
