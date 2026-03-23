#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <codex-session-id> [workdir]" >&2
  exit 1
fi

SESSION_ID="$1"
WORKDIR="${2:-$(pwd)}"
TMUX_SESSION="codex_ctx_check_$$"

cleanup() {
  tmux kill-session -t "$TMUX_SESSION" >/dev/null 2>&1 || true
}
trap cleanup EXIT

tmux new-session -d -s "$TMUX_SESSION" "bash -lc 'cd \"$WORKDIR\" && codex resume \"$SESSION_ID\" --no-alt-screen'"
sleep 3

pane="$(tmux capture-pane -pt "$TMUX_SESSION" -S -60 || true)"
if grep -q 'Update available!' <<<"$pane"; then
  tmux send-keys -t "$TMUX_SESSION" 2 Enter
  sleep 4
  pane="$(tmux capture-pane -pt "$TMUX_SESSION" -S -80 || true)"
fi

status_line="$(grep -E 'gpt-[^·]+ · .*% left · ' <<<"$pane" | tail -n 1 || true)"
if [[ -z "$status_line" ]]; then
  echo "context-window footer not found" >&2
  printf '%s\n' "$pane" >&2
  exit 2
fi

percent_left="$(grep -oE '[0-9]+% left' <<<"$status_line" | tail -n 1 | awk '{print $1}' | tr -d '%')"
percent_used="$((100 - percent_left))"

printf 'status_line: %s\n' "$status_line"
printf 'left_percent: %s\n' "$percent_left"
printf 'used_percent: %s\n' "$percent_used"
