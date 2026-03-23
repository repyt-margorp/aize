#!/usr/bin/env bash
set -euo pipefail

SESSION_NAME="${SESSION_NAME:-agent-chat-demo}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKDIR="${WORKDIR:-$SCRIPT_DIR}"
RUN_DIR="${RUN_DIR:-$WORKDIR/.tmux-agent-chat}"

CODEX_SYSTEM="You are CodexFox. You are concise, curious, and friendly. Stay in character. Keep each reply to one short sentence, ask at most one simple question, and never mention being an AI."
CLAUDE_SYSTEM="You are ClaudeOtter. You are calm, playful, and slightly skeptical. Stay in character. Keep each reply to one short sentence, ask at most one simple question, and never mention being an AI."

mkdir -p "$RUN_DIR"
rm -f "$RUN_DIR"/turn*.out "$RUN_DIR"/turn*.cmd "$RUN_DIR"/summary.txt

wait_for_file() {
  local path="$1"
  local timeout="${2:-90}"
  local start
  start="$(date +%s)"

  while true; do
    if [ -s "$path" ]; then
      return 0
    fi

    if (( "$(date +%s)" - start >= timeout )); then
      echo "Timed out waiting for $path" >&2
      return 1
    fi

    sleep 1
  done
}

wait_for_prompt() {
  local pane="$1"
  local timeout="${2:-60}"
  local start
  start="$(date +%s)"

  while true; do
    if tmux capture-pane -pt "$pane" -S -30 | grep -Eq 'paneA\$|paneB\$'; then
      return 0
    fi

    if (( "$(date +%s)" - start >= timeout )); then
      echo "Timed out waiting for shell prompt in $pane" >&2
      tmux capture-pane -pt "$pane" -S -50 >&2 || true
      return 1
    fi

    sleep 1
  done
}

shell_quote() {
  printf "%q" "$1"
}

send_command() {
  local pane="$1"
  local command="$2"

  wait_for_prompt "$pane"
  tmux send-keys -t "$pane" C-c
  sleep 0.2
  tmux send-keys -t "$pane" "$command"
  tmux send-keys -t "$pane" Enter
}

read_trimmed() {
  python3 - "$1" <<'PY'
from pathlib import Path
import sys
text = Path(sys.argv[1]).read_text().strip()
print(text)
PY
}

run_codex_turn() {
  local turn="$1"
  local prompt="$2"
  local outfile="$RUN_DIR/turn${turn}_codex.out"
  local cmd

  rm -f "$outfile"
  cmd="cd $(shell_quote "$WORKDIR") && codex exec --dangerously-bypass-approvals-and-sandbox $(shell_quote "$prompt") > $(shell_quote "$outfile")"
  printf '%s\n' "$cmd" > "$RUN_DIR/turn${turn}_codex.cmd"
  send_command "$CODEX_PANE" "$cmd"
  wait_for_file "$outfile"
  read_trimmed "$outfile"
}

run_claude_turn() {
  local turn="$1"
  local prompt="$2"
  local outfile="$RUN_DIR/turn${turn}_claude.out"
  local cmd

  rm -f "$outfile"
  cmd="cd $(shell_quote "$WORKDIR") && claude -p --dangerously-skip-permissions $(shell_quote "$prompt") > $(shell_quote "$outfile")"
  printf '%s\n' "$cmd" > "$RUN_DIR/turn${turn}_claude.cmd"
  send_command "$CLAUDE_PANE" "$cmd"
  wait_for_file "$outfile"
  read_trimmed "$outfile"
}

tmux kill-session -t "$SESSION_NAME" 2>/dev/null || true
CODEX_PANE="$(tmux new-session -d -P -F '#{pane_id}' -s "$SESSION_NAME" -x 180 -y 50 "cd '$WORKDIR' && PS1='paneA$ ' bash --noprofile --norc")"
CLAUDE_PANE="$(tmux split-window -h -P -F '#{pane_id}' -t "$SESSION_NAME:0" "cd '$WORKDIR' && PS1='paneB$ ' bash --noprofile --norc")"
tmux select-layout -t "$SESSION_NAME:0" even-horizontal

wait_for_prompt "$CODEX_PANE"
wait_for_prompt "$CLAUDE_PANE"

codex_msg="Hello ClaudeOtter, I am CodexFox. Please introduce yourself in one short sentence."
claude_msg="Hello CodexFox, I am ClaudeOtter. Please introduce yourself in one short sentence."

{
  echo "Character setup"
  echo "Codex: $CODEX_SYSTEM"
  echo "Claude: $CLAUDE_SYSTEM"
  echo
} > "$RUN_DIR/summary.txt"

for turn in 1 2 3; do
  codex_prompt="$CODEX_SYSTEM"$'\n'"You are talking to ClaudeOtter. This is turn $turn of 3."$'\n'"ClaudeOtter just said: $claude_msg"
  codex_reply="$(run_codex_turn "$turn" "$codex_prompt")"
  printf 'Turn %s Codex -> Claude: %s\n' "$turn" "$codex_reply" >> "$RUN_DIR/summary.txt"

  claude_prompt="$CLAUDE_SYSTEM"$'\n'"You are talking to CodexFox. This is turn $turn of 3."$'\n'"CodexFox just said: $codex_reply"
  claude_reply="$(run_claude_turn "$turn" "$claude_prompt")"
  printf 'Turn %s Claude -> Codex: %s\n' "$turn" "$claude_reply" >> "$RUN_DIR/summary.txt"

  codex_msg="$codex_reply"
  claude_msg="$claude_reply"
done

echo >> "$RUN_DIR/summary.txt"
echo "tmux session: $SESSION_NAME" >> "$RUN_DIR/summary.txt"
echo "codex pane: $CODEX_PANE" >> "$RUN_DIR/summary.txt"
echo "claude pane: $CLAUDE_PANE" >> "$RUN_DIR/summary.txt"

cat "$RUN_DIR/summary.txt"
