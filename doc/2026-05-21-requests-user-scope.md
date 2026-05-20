# Requests User Scope Fix

## User-visible behavior

HTTPBridge Requests now loads request-bearing session summaries for the active session owner when a superuser opens another user's session. A root/superuser viewer on a `repyt` session no longer sees root-owned summaries in the owned Requests view while repyt's human-facing requests are hidden.

## Files touched

- `src/runtime/http_handler.py`
  - Uses the resolved active session username as the owned-scope summary source for initial page summaries, `/sessions`, and `/overview`.
  - Keeps `viewer_username` for display/auth identity and leaves `scope=all` behavior unchanged.
- `tests/test_http_handler_goal_save.py`
  - Adds a regression test for a root viewer opened on a repyt session with a pending user-response request.

## Verification

- `python3 -m unittest tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_cross_user_session_view_lists_session_owner_requests_for_owned_scope`
- `python3 -m unittest tests.test_entrance_page.EntrancePageTests.test_ui_history_includes_user_response_wait_started_event`
- Restarted via `./restart_aize_unit.sh`.
- Verified runtime `/sessions?session_id=<repyt-session>&session_window_seconds=0` returns `username=repyt`, `viewer_username=root`, `scope=owned`, includes `user-response-browser-verify`, and includes no root-owned summaries.
- Verified in headless Chrome over DevTools: clicked the HTTPBridge Requests button and confirmed the Requests pane rendered `user-response-browser-verify` with `status: waiting / active` and the human-facing prompt text.

## Remaining risk

The verification used a synthetic repyt request created directly in runtime state to isolate rendering and scoping. The GoalManager-created path was inspected in code and covered by existing persistence/event tests, but this run did not wait for a live GoalManager model turn to generate a new request naturally.
