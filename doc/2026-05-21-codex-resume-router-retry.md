# Codex Resume Router Retry

## Behavior changed

- Codex resume now treats the internal `write_stdin failed: Unknown process id ...` tool-router failure as a stale-resume condition.
- When that error occurs during `codex exec resume`, the runtime retries the turn without reusing the old provider session instead of surfacing a worker panic immediately.

## Files touched

- `src/runtime/providers/codex.py`
- `tests/test_goal_manager_compact.py`

## Verification

- `python3 -m unittest tests.test_goal_manager_compact.GoalManagerCompactTests.test_run_codex_retries_without_session_when_resume_hits_internal_tool_process_error -v`

## Residual risk

- This recovers the observed Codex resume failure path, but other non-session-related Codex CLI failures still surface as worker failures by design.
