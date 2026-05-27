## Summary

Adjusted GoalManager compact persistence so a successful compact check does not leave the session mirror stuck in a stale `needs_compact` state when the authoritative service audit is already `all_clear`.

## Files Touched

- `src/runtime/goal_persist.py`
- `tests/test_goal_manager_compact.py`

## Behavior Change

- After `service.goal_manager_compact_checked` with real compaction completion, GoalManager now rewrites the persisted runtime summary to a healthy idle/no-work state when the bound service audit file is already `all_clear`.
- This prevents repeated resident follow-up loops that were driven by stale GoalManager summary text rather than actual pending work.

## Verification

- `python3 -m unittest tests.test_goal_manager_compact.GoalManagerCompactTests.test_handle_goal_manager_compact_request_calls_compactor_when_toggle_on tests.test_goal_manager_compact.GoalManagerCompactTests.test_handle_goal_manager_compact_request_reconciles_stale_summary_when_audit_is_clear tests.test_goal_manager_compact.GoalManagerCompactTests.test_handle_goal_manager_compact_request_keeps_noninteractive_helper_skip_as_checked -q`
- `python3 -m py_compile src/runtime/goal_persist.py tests/test_goal_manager_compact.py`

## Remaining Risk

- Existing resident processes need one fresh compact persistence pass or a one-time state reconcile to clear already-written stale summaries. The code change prevents new occurrences.
