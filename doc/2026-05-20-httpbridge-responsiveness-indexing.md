## Summary
- Reduced HttpBridge overview and workspace-detail latency by moving session activity and runtime-journal lookup onto session-level metadata, while keeping full timeline and runtime-log data in their existing per-session files for on-demand expansion.
- Added shared hour/day time-window filtering for WorkspaceView message fetches and SessionLog fetches so the UI can request narrower slices without reopening all history by default.

## Files Touched
- `src/runtime/persistent_state_pkg/history.py`
- `src/runtime/persistent_state_pkg/_core.py`
- `src/runtime/http_handler.py`
- `src/runtime/html_renderer.py`
- `tests/test_goal_manager_compact.py`
- `tests/test_http_handler_goal_save.py`
- `AGENTS.md`

## Verification
- `python3 -m unittest tests.test_goal_manager_compact tests.test_http_handler_goal_save`

## Remaining Risk
- Existing sessions created before this change only gain `activity_index` and `runtime_journal_summary` as new history arrives or when their metadata is rewritten, so older sessions may still fall back to sparse/default index metadata until they receive another event.
