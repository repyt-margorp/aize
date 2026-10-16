#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <codex-session-id> [workdir] [threshold-left-percent]" >&2
  exit 1
fi

SESSION_ID="$1"
WORKDIR="${2:-$(pwd)}"
THRESHOLD_LEFT_PERCENT="${3:-101}"

TMP_STDOUT="$(mktemp)"
TMP_STDERR="$(mktemp)"
cleanup() {
  rm -f "$TMP_STDOUT" "$TMP_STDERR"
}
trap cleanup EXIT

set +e
(
  cd "$WORKDIR"
  codex exec resume --dangerously-bypass-approvals-and-sandbox --json "$SESSION_ID" "/compact"
) < /dev/null >"$TMP_STDOUT" 2>"$TMP_STDERR"
RC=$?
set -e

printf 'threshold_left_percent: %s\n' "$THRESHOLD_LEFT_PERCENT"

if [[ $RC -ne 0 ]]; then
  printf 'command_status: failed\n'
  printf 'compaction: failed\n'
  stderr_text="$(tr '\n' ' ' <"$TMP_STDERR" | sed 's/[[:space:]]\+/ /g; s/^ //; s/ $//')"
  if [[ -n "$stderr_text" ]]; then
    printf 'stderr: %s\n' "$stderr_text"
  fi
  exit "$RC"
fi

if ! grep -q '"type":"turn.completed"' "$TMP_STDOUT"; then
  printf 'command_status: incomplete\n'
  printf 'compaction: unconfirmed\n'
  stderr_text="$(tr '\n' ' ' <"$TMP_STDERR" | sed 's/[[:space:]]\+/ /g; s/^ //; s/ $//')"
  if [[ -n "$stderr_text" ]]; then
    printf 'stderr: %s\n' "$stderr_text"
  fi
  exit 5
fi

printf 'command_status: accepted\n'
printf 'compaction: triggered\n'
printf 'wait_status: turn_completed\n'
