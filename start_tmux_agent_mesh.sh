#!/usr/bin/env bash
set -euo pipefail

SESSION_NAME="${SESSION_NAME:-agent-mesh-demo}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKDIR="${WORKDIR:-$SCRIPT_DIR}"
RUN_DIR="${RUN_DIR:-$WORKDIR/.tmux-agent-mesh}"
MAX_TURNS="${MAX_TURNS:-3}"
RUN_ID="${RUN_ID:-run-$(date +%Y%m%d-%H%M%S)}"
MANIFEST_PATH="$RUN_DIR/manifest.json"
INITIAL_MESSAGE="${INITIAL_MESSAGE:-Hello ClaudeOtter, give one short self-introduction and one playful question.}"

mkdir -p "$RUN_DIR"
rm -f "$RUN_DIR"/*.json "$RUN_DIR"/*.jsonl "$RUN_DIR"/summary.txt

wait_for_text() {
  local pane="$1"
  local expected="$2"
  local timeout="${3:-30}"
  local start
  start="$(date +%s)"

  while true; do
    if tmux capture-pane -pt "$pane" -S -80 | grep -Fq "$expected"; then
      return 0
    fi
    if (( "$(date +%s)" - start >= timeout )); then
      echo "Timed out waiting for '$expected' in $pane" >&2
      tmux capture-pane -pt "$pane" -S -120 >&2 || true
      return 1
    fi
    sleep 1
  done
}

count_done() {
  local log_path="$1"
  if [ ! -f "$log_path" ]; then
    echo 0
    return
  fi
  python3 - "$log_path" <<'PY'
import json
import sys
count = 0
with open(sys.argv[1], encoding="utf-8") as fh:
    for line in fh:
        if not line.strip():
            continue
        if json.loads(line)["phase"] == "done":
            count += 1
print(count)
PY
}

chmod +x "$WORKDIR/tmux_route_send.sh" "$WORKDIR/tmux_structured_agent_loop.sh"
tmux kill-session -t "$SESSION_NAME" 2>/dev/null || true

CODEX_PANE="$(tmux new-session -d -P -F '#{pane_id}' -s "$SESSION_NAME" -x 180 -y 50 "cd '$WORKDIR' && PS1='mesh-codex$ ' bash --noprofile --norc")"
CLAUDE_PANE="$(tmux split-window -h -P -F '#{pane_id}' -t "$SESSION_NAME:0" "cd '$WORKDIR' && PS1='mesh-claude$ ' bash --noprofile --norc")"
tmux select-layout -t "$SESSION_NAME:0" even-horizontal

python3 - "$MANIFEST_PATH" "$RUN_ID" "$MAX_TURNS" "$CODEX_PANE" "$CLAUDE_PANE" <<'PY'
import json
import sys
from pathlib import Path

manifest = {
    "run_id": sys.argv[2],
    "max_turns": int(sys.argv[3]),
    "agents": [
        {
            "agent_id": "agent-codex-001",
            "kind": "codex",
            "display_name": "CodexFox",
            "persona": "You are concise, curious, and friendly. Your role is to ask ClaudeOtter to do web searches when outside facts are needed, then react to ClaudeOtter's reported findings and steer the conversation to a conclusion.",
            "pane_id": sys.argv[4],
            "max_turns": int(sys.argv[3]),
        },
        {
            "agent_id": "agent-claude-001",
            "kind": "claude",
            "display_name": "ClaudeOtter",
            "persona": "You are calm, playful, and slightly skeptical. Your role is to perform web searches when CodexFox asks for outside facts, then report the findings with a source link before continuing the conversation.",
            "pane_id": sys.argv[5],
            "max_turns": int(sys.argv[3]),
        },
    ],
    "routes": [
        {
            "route_id": "route-codex-to-claude",
            "sender_id": "agent-codex-001",
            "recipient_id": "agent-claude-001",
            "enabled": True,
        },
        {
            "route_id": "route-claude-to-codex",
            "sender_id": "agent-claude-001",
            "recipient_id": "agent-codex-001",
            "enabled": True,
        },
    ],
}
Path(sys.argv[1]).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

tmux send-keys -t "$CODEX_PANE" "cd '$WORKDIR' && WORKDIR='$WORKDIR' RUN_DIR='$RUN_DIR' '$WORKDIR/tmux_structured_agent_loop.sh' '$MANIFEST_PATH' 'agent-codex-001' '$RUN_DIR/agent-codex-001.jsonl'" Enter
tmux send-keys -t "$CLAUDE_PANE" "cd '$WORKDIR' && WORKDIR='$WORKDIR' RUN_DIR='$RUN_DIR' '$WORKDIR/tmux_structured_agent_loop.sh' '$MANIFEST_PATH' 'agent-claude-001' '$RUN_DIR/agent-claude-001.jsonl'" Enter

wait_for_text "$CODEX_PANE" "[agent-codex-001] READY"
wait_for_text "$CLAUDE_PANE" "[agent-claude-001] READY"

tmux send-keys -t "$CODEX_PANE" -l "$INITIAL_MESSAGE"
tmux send-keys -t "$CODEX_PANE" Enter

start="$(date +%s)"
while true; do
  codex_done="$(count_done "$RUN_DIR/agent-codex-001.jsonl")"
  claude_done="$(count_done "$RUN_DIR/agent-claude-001.jsonl")"
  if [ "$codex_done" -ge 1 ] && [ "$claude_done" -ge 1 ]; then
    break
  fi
  if (( "$(date +%s)" - start >= 300 )); then
    echo "Timed out waiting for agent mesh completion" >&2
    break
  fi
  sleep 1
done

python3 - "$MANIFEST_PATH" "$RUN_DIR/agent-codex-001.jsonl" "$RUN_DIR/agent-claude-001.jsonl" "$RUN_DIR/summary.txt" <<'PY'
import json
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
logs = [Path(sys.argv[2]), Path(sys.argv[3])]
summary = Path(sys.argv[4])

lines = [
    f"run_id: {manifest['run_id']}",
    f"tmux session: agent-mesh-demo",
]
for agent in manifest["agents"]:
    lines.append(f"{agent['agent_id']} ({agent['display_name']}): pane {agent['pane_id']}")

lines.append("")
for log in logs:
    entries = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    lines.append(log.name)
    for entry in entries:
        lines.append(
            f"  turn={entry['turn']} phase={entry['phase']} text={entry['text']}"
        )
    lines.append("")

summary.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
print(summary.read_text(encoding="utf-8"), end="")
PY
