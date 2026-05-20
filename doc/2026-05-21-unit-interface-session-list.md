# Unit Interface Session List

- The Units pane now includes a per-unit launched-session list with each session label, session id, latest update time, and direct actions to open the unit interface first or fall back to WorkspaceView.
- The selected unit's `Open Last Session` action now prefers the unit interface URL with `session_id=...` when that unit exposes a web interface, so Entrance opens the corresponding Entrance UI instead of dropping straight into WorkspaceView.
- The `/units` payload now includes `launched_sessions` and `launched_session_count` for the current user, sourced from sessions associated with each unit.

Files touched: `src/runtime/http_handler.py`, `tests/test_http_handler_goal_save.py`

Verification:
- `python3 -m unittest tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_get_units_includes_unit_launched_sessions tests.test_entrance_page.EntrancePageTests.test_main_page_renders_latest_first_workspace_header_and_fixed_composer tests.test_session_listing -v`
- Headless Chrome against a local mocked HttpBridge page confirmed the rendered DOM contained `Ops Entrance`, `Build Entrance`, `Open Unit Interface`, `Open Workspace`, `Open Latest Unit Session`, and `Launched Sessions`.

Remaining risk:
- Browser verification used a mocked `/units` payload and renderer page rather than a restarted full runtime, so live-auth/session plumbing should still be watched during the next normal restart.
