#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <claude-session-id> [workdir]" >&2
  exit 1
fi

SESSION_ID="$1"
WORKDIR="${2:-$(pwd)}"

# Use stream-json probe to get real token usage from modelUsage.contextWindow.
# More reliable than TUI footer parsing (footer format changed in Claude Code v2.x).
# Note: adds a minimal turn (~10 tokens) to the session history.
probe_output=""
probe_rc=0
probe_output="$(cd "$WORKDIR" && timeout 90 claude -p "reply with one word: OK" \
  --dangerously-skip-permissions \
  --output-format stream-json \
  --verbose \
  --resume "$SESSION_ID" 2>/dev/null)" || probe_rc=$?

if [[ -z "$probe_output" ]]; then
  echo "context-window footer not found" >&2
  echo "probe produced no output (rc=$probe_rc)" >&2
  exit 2
fi

_pycode='
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        ev = json.loads(line)
    except Exception:
        continue
    if ev.get("type") == "result" and isinstance(ev.get("modelUsage"), dict):
        mu = ev["modelUsage"]
        for model_name, stats in mu.items():
            cw = stats.get("contextWindow", 0)
            if not cw:
                continue
            input_t = stats.get("inputTokens", 0) or 0
            cache_read = stats.get("cacheReadInputTokens", 0) or 0
            cache_create = stats.get("cacheCreationInputTokens", 0) or 0
            output_t = stats.get("outputTokens", 0) or 0
            total_used = input_t + cache_read + cache_create + output_t
            used_pct = round(total_used * 100 / cw)
            left_pct = 100 - used_pct
            print("model: " + model_name)
            print("context_window: " + str(cw))
            print("total_tokens: " + str(total_used))
            print("left_percent: " + str(left_pct))
            print("used_percent: " + str(used_pct))
            sys.exit(0)
'

model_usage="$(printf '%s\n' "$probe_output" | python3 -c "$_pycode")" || true

if [[ -z "$model_usage" ]]; then
  echo "context-window footer not found" >&2
  printf '%s\n' "$probe_output" | tail -5 >&2
  exit 2
fi

used_percent="$(printf '%s\n' "$model_usage" | grep '^used_percent:' | awk '{print $2}')"
left_percent="$(printf '%s\n' "$model_usage" | grep '^left_percent:' | awk '{print $2}')"

if [[ -z "$used_percent" || -z "$left_percent" ]]; then
  echo "used percent not found" >&2
  printf '%s\n' "$model_usage" >&2
  exit 3
fi

status_line="${used_percent}% context used"

printf 'status_line: %s\n' "$status_line"
printf 'left_percent: %s\n' "$left_percent"
printf 'used_percent: %s\n' "$used_percent"
