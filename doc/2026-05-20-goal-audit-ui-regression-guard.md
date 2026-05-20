## Summary

- Restored explicit `Goal Active` / `Goal Inactive` wording in the main session status rendering so the goal-state labels remain visible instead of collapsing to bare `Active` / `Inactive`.
- Kept the session page and runtime journal UI on the summary-first path while preserving detailed per-agent `Event Log` and `Raw JSON` expansion in the rendered page.
- Tightened GoalManager and AIzeDevelopment hierarchy text so completion now requires both the requested positive result and explicit regression checks for adjacent behavior.

## Files Touched

- `src/runtime/html_renderer.py`
- `src/runtime/goal_audit.py`
- `plugins/aize-development/units/bug-hunting/unit.json`
- `plugins/aize-entrance/units/entrance/unit.json`
- `tests/test_entrance_page.py`
- `tests/test_goal_manager_compact.py`

## Verification

- `python3 -m unittest tests.test_entrance_page tests.test_http_handler_goal_save tests.test_goal_manager_compact`
- Separate-port runtime verification on `http://127.0.0.1:<alternate-port>` with runtime root `./.temp/runtime-ui-regression-verify`
- Verified `/?session_id=386fccc83882e463` contained `Goal Active`, `Goal In Progress`, `Runtime Event Log`, `Event Log`, and `Raw JSON`
- Verified `/session/runtime-log?session_id=386fccc83882e463&limit=5` returned `200` with summary `{"entry_count": 1, "service_ids": ["service-codex-verify"], "event_types": ["runtime.status_changed"]}`
- Stopped the temporary runtime after verification; no cutover was performed

## Residual Risk

- The status/event-log fix is covered at the renderer-string level plus one isolated runtime fetch path. A full browser-driven expansion/collapse interaction pass would still be useful before any production cutover.
