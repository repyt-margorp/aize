#!/usr/bin/env bash
set -euo pipefail

SESSION_NAME="${SESSION_NAME:-codex-bridge-demo}"
WORKDIR="${WORKDIR:-$(cd "$(dirname "$0")" && pwd)}"
CODEX_CMD="${CODEX_CMD:-codex --dangerously-bypass-approvals-and-sandbox --no-alt-screen}"
PAYLOAD="SOURCE_PAYLOAD_$(date +%s)"
FORWARDED="FORWARDED_${PAYLOAD}"

wait_for_text() {
  local pane="$1"
  local expected="$2"
  local timeout="${3:-60}"
  local start
  start="$(date +%s)"

  while true; do
    if tmux capture-pane -pt "$pane" -S -200 | grep -Fq "$expected"; then
      return 0
    fi

    if (( "$(date +%s)" - start >= timeout )); then
      echo "Timed out waiting for '$expected' in $pane" >&2
      tmux capture-pane -pt "$pane" -S -200 >&2 || true
      return 1
    fi

    sleep 1
  done
}

send_prompt() {
  local pane="$1"
  local prompt="$2"

  tmux send-keys -t "$pane" C-c
  sleep 1
  tmux send-keys -t "$pane" "$prompt"
  tmux send-keys -t "$pane" Enter
  sleep 0.3
  tmux send-keys -t "$pane" Enter
}

tmux kill-session -t "$SESSION_NAME" 2>/dev/null || true
SOURCE_PANE="$(tmux new-session -d -P -F '#{pane_id}' -s "$SESSION_NAME" -x 160 -y 50 "cd '$WORKDIR' && $CODEX_CMD")"
TARGET_PANE="$(tmux split-window -h -P -F '#{pane_id}' -t "$SESSION_NAME:0" "cd '$WORKDIR' && $CODEX_CMD")"
tmux select-layout -t "$SESSION_NAME:0" even-horizontal

wait_for_text "$SOURCE_PANE" "gpt-5.4"
wait_for_text "$TARGET_PANE" "gpt-5.4"

send_prompt "$TARGET_PANE" "You will receive one payload from another Codex pane. When it arrives, reply with exactly: $FORWARDED"
wait_for_text "$TARGET_PANE" "exactly: $FORWARDED"

send_prompt "$SOURCE_PANE" "Reply with exactly: $PAYLOAD"
wait_for_text "$SOURCE_PANE" "• $PAYLOAD"

send_prompt "$TARGET_PANE" "The other Codex pane replied with this exact payload: $PAYLOAD"
wait_for_text "$TARGET_PANE" "• $FORWARDED"

echo "Session: $SESSION_NAME"
echo "Source pane reply: $PAYLOAD"
echo "Forwarded to target pane and confirmed reply: $FORWARDED"
echo
echo "Inspect live panes with:"
echo "  tmux attach -t $SESSION_NAME"
