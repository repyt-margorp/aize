# User Response Request UI

## Behavior Changed

UserResponseRequest wait records created by GoalManager now surface in the user-visible HTTPBridge UI immediately and after refresh. The workspace status strip, goal metadata, request board, session map summaries, and timeline can show an active wait request such as `Waiting for User` with the request text and request id.

## Cause

Request creation and persistence were already working: `run_goal_audit()` parsed `user_response_request` records, `AgentService` persisted them through `update_session_user_response_wait()`, and HTTPBridge goal-state payloads included the `user_response_wait_*` fields. The UI failure was in rendering/state propagation: `refreshGoalState()` copied only goal/runtime fields and dropped the persisted wait fields, realtime workspace summary patching ignored `service.user_response_wait_*` events, and UI history filtering excluded the `service.user_response_wait_started` event.

## Files Touched

- `src/runtime/html_renderer.py`
- `src/runtime/ui_history.py`
- `tests/test_entrance_page.py`
- `doc/2026-05-21-user-response-request-ui.md`

## Verification

- `python3 -m py_compile src/runtime/html_renderer.py src/runtime/ui_history.py`
- `python3 -m unittest tests.test_entrance_page.EntrancePageTests.test_main_page_renders_latest_first_workspace_header_and_fixed_composer tests.test_entrance_page.EntrancePageTests.test_ui_history_includes_user_response_wait_started_event tests.test_goal_manager_compact.GoalManagerCompactTests.test_goal_state_response_payload_includes_user_response_wait_fields tests.test_goal_manager_compact.GoalManagerCompactTests.test_run_goal_audit_parses_user_response_request`
- Restarted with `./restart_aize_unit.sh`.
- Browser-verified with headless Chrome over the live HTTPS HttpBridge on `https://127.0.0.1:64123/`: a persisted GoalManager-originated request in session `5671d07323499e3c` rendered `Waiting for User`, request id `user-response-a92dcbf2f7cd`, and question text `Which deployment region should the worker use?`. Screenshot artifact: `.temp/user-response-ui-browser/cdp-session.png`.

## Residual Risk

The browser verification used a locally seeded persisted wait record to avoid waiting for a live model audit to choose a user-response request. The creation path is covered by existing and focused unit tests, but full live LLM behavior still depends on GoalManager producing the `user_response_request` audit record for an ambiguous worker-originated task.
