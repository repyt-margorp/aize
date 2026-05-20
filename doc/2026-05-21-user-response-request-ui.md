# User Response Request UI

## Behavior Changed

UserResponseRequest wait records created by GoalManager now surface in the user-visible HTTPBridge UI immediately and after refresh. The workspace status strip, goal metadata, request board, session map summaries, and timeline can show an active wait request such as `Waiting for User` with the request text and request id.

The Requests toolbar button now refreshes the live session index before rendering the request board, so a human-facing request created after the page was loaded is not hidden behind stale Root/current-session-only page state.

The Requests pane now renders per-request history instead of only the last session-level wait snapshot. Each request card shows the question, background/reason, timeout window, exact deadline, deterministic timeout policy, and request/source metadata so a human can answer with enough context without manually reconstructing every agent turn.

## Cause

Request creation and persistence were already working: `run_goal_audit()` parsed `user_response_request` records, `AgentService` persisted them through `update_session_user_response_wait()`, and HTTPBridge goal-state payloads included the `user_response_wait_*` fields. The UI failure was in rendering/state propagation: `refreshGoalState()` copied only goal/runtime fields and dropped the persisted wait fields, realtime workspace summary patching ignored `service.user_response_wait_*` events, and UI history filtering excluded the `service.user_response_wait_started` event.

The remaining Requests-button failure was a stale client-side source list. `renderUserRequests()` read only the current `workspaceSummaries` array. When the user was on the Root/map-only UI, that array could contain only the initial Root/current visible sessions; pressing Requests rendered that stale array directly instead of first asking `/sessions` for the latest visible sessions. The request object, persisted wait fields, and timeline event existed, but the request session was absent from the client array used by the Requests pane.

## Files Touched

- `src/runtime/agent_service.py`
- `src/runtime/http_handler.py`
- `src/runtime/session_view.py`
- `src/runtime/html_renderer.py`
- `src/runtime/ui_history.py`
- `tests/test_entrance_page.py`
- `tests/test_session_listing.py`
- `doc/2026-05-21-user-response-request-ui.md`

## Verification

- `python3 -m py_compile src/runtime/html_renderer.py src/runtime/ui_history.py`
- `python3 -m unittest tests.test_entrance_page.EntrancePageTests.test_main_page_renders_latest_first_workspace_header_and_fixed_composer tests.test_entrance_page.EntrancePageTests.test_ui_history_includes_user_response_wait_started_event tests.test_goal_manager_compact.GoalManagerCompactTests.test_goal_state_response_payload_includes_user_response_wait_fields tests.test_goal_manager_compact.GoalManagerCompactTests.test_run_goal_audit_parses_user_response_request`
- `python3 -m py_compile src/runtime/html_renderer.py src/runtime/session_view.py src/runtime/http_handler.py src/runtime/agent_service.py`
- `python3 -m unittest tests.test_entrance_page.EntrancePageTests.test_main_page_renders_latest_first_workspace_header_and_fixed_composer tests.test_session_listing.SessionListingTests.test_session_summary_exposes_user_response_request_history tests.test_goal_manager_compact.GoalManagerCompactTests.test_goal_state_response_payload_includes_user_response_wait_fields -v`
- Restarted with `./restart_aize_unit.sh`.
- Browser-verified with headless Chrome over the live HTTPS HttpBridge: a persisted GoalManager-originated request rendered `Waiting for User`, its request id, and its question text. Screenshot artifact: `.temp/user-response-ui-browser/cdp-session.png`.
- Reproduced the Requests-button stale-state failure by loading HTTPBridge Root first, then creating a fresh GoalManager-style wait record for a regular user session after page load. Before clicking Requests, the page did not contain the new request id; after the fix, clicking Requests fetched the live session index and rendered the request title, `status: waiting / active`, request id, and question text. Browser artifacts: `.temp/requests-flow-browser/requests-pane.png`, `.temp/requests-flow-browser/requests-dom.html`, `.temp/requests-flow-browser/messages.json`.
- Browser-verified the richer request-card layout with a static rendered HTTPBridge page opened in headless Chrome. The Requests pane rendered both a timed-out request and a live waiting request with `Question`, `Background`, `Timeout`, `Timeout policy`, `Requested by`, and `Source` lines. Artifacts: `.temp/requests-pane-rich-browser/requests-pane.png`, `.temp/requests-pane-rich-browser/requests-dom.html`.
- `python3 -m unittest tests.test_entrance_page.EntrancePageTests.test_main_page_renders_latest_first_workspace_header_and_fixed_composer tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_cross_user_session_view_lists_session_owner_requests_for_owned_scope`

## Residual Risk

The richer card-layout verification used a static file render because the check was about visible request copy and field presence, not auth/session transport. That static page hits a `history.replaceState` browser-origin restriction after the Requests pane opens under `file://`, but the rendered pane content and screenshot were captured before that warning and the live HTTPBridge behavior remains covered by the earlier runtime verification above.
