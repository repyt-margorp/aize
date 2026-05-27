# Requests Owned Scope Regression

## Behavior Changed

The HTTPBridge Requests pane now honors the current session scope when refreshing request records. A superuser no longer forces `scope=all` every time the Requests button is pressed; owned-scope views stay scoped to the resolved active session owner, so repyt-owned human response requests remain visible from repyt Session UI instead of being mixed into or hidden behind Root/global results.

## Cause

Request creation and persistence were intact. The concrete repyt request `user-response-6ed5721f0794` was present in `repyt/0ac1231110d2881f/session.json`, including request history, prompt text, source service, and timeout state.

The UI refresh query regressed in `requestDisplayQuery()`: it appended `scope=all` for every superuser instead of using the current `sessionScopeQuery()`. That bypassed the owned-scope path previously added for cross-user Session UI and could surface Root/global sessions while the human-facing owner-specific request set was not the effective Requests view.

## Files Touched

- `src/runtime/html_renderer.py`
- `tests/test_entrance_page.py`

## Verification

- `python3 -m py_compile src/runtime/html_renderer.py`
- `python3 -m unittest tests.test_entrance_page.EntrancePageTests.test_main_page_renders_latest_first_workspace_header_and_fixed_composer tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_cross_user_session_view_lists_session_owner_requests_for_owned_scope -v`
- Restarted with `./restart_aize_unit.sh`; live health returned `200 OK` on `https://127.0.0.1:64123/health`.
- Live API check against `GET /sessions?session_id=0ac1231110d2881f&session_window_seconds=604800` as user `repyt` returned `scope=owned`, `username=repyt`, and the request-bearing session with `user-response-6ed5721f0794`.
- Browser verification with headless Chrome clicked the real HTTPBridge `Requests` button on `https://127.0.0.1:64123/?session_id=0ac1231110d2881f&session_window_seconds=604800` and confirmed the Requests pane was visible and contained `user-response-6ed5721f0794` plus the prompt text `Provide the next concrete AIze development task...`.
- Browser artifacts: `.temp/requests-scope-verify/requests-pane.png` and `.temp/requests-scope-verify/requests-dom.json`.

## Residual Risk

The verified request was already timed out, not actively waiting. This still covers the failed surfacing path because the Requests pane is expected to show recent request records as well as active waits. A fresh active request would exercise the same query and rendering path but may be subject to live GoalManager timing.
