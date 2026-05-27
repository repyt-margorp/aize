# Worker User Response Request UI Fallback

## Behavior Changed

Worker-originated user response request records now surface in the user-visible Requests pane even when the workspace summary refresh has not populated request history. The active session renderer falls back to the session-local `userResponseWait*` state, so recorded worker requests display their request id, question, recorded status, timeout, and worker role from the page's initial session state.

Worker-originated requests remain non-authoritative: they are recorded as `service.user_response_wait_ignored` and do not start an active user wait. GoalManager remains the only role allowed to create a blocking wait.

## Cause

Request creation and persistence were functioning: `record_session_user_response_request()` stored the request, `/session/goal/state` returned `user_response_wait_status="recorded"`, and the active page state contained the request id and question. The UI failure was in Entrance/Workspace rendering. The Requests pane rendered only from `workspaceSummaries` or the live `/sessions` refresh result. If that summary path had not supplied the active session's request history, the pane showed `0 request records visible` even while the active session state already held a recorded worker request.

## Files Touched

- `src/runtime/html_renderer.py`
- `tests/test_entrance_page.py`

## Verification

- `python3 -m py_compile src/runtime/html_renderer.py`
- `python3 -m unittest tests.test_entrance_page.EntrancePageTests.test_main_page_renders_latest_first_workspace_header_and_fixed_composer tests.test_entrance_page.EntrancePageTests.test_entrance_page_renders_chat_polling_surface -v`
- `python3 -m unittest tests.test_goal_manager_compact.GoalManagerCompactTests.test_record_session_user_response_request_sets_visible_summary_fields tests.test_goal_manager_compact.GoalManagerCompactTests.test_run_goal_audit_parses_user_response_request tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_cross_user_session_view_lists_session_owner_requests_for_owned_scope tests.test_entrance_page.EntrancePageTests.test_main_page_renders_latest_first_workspace_header_and_fixed_composer tests.test_entrance_page.EntrancePageTests.test_entrance_page_renders_chat_polling_surface -v`
- Restarted with `./restart_aize_unit.sh`.
- Browser verification against live HttpBridge at `https://127.0.0.1:64123`: seeded a worker-originated recorded request via `record_session_user_response_request()` plus a `service.user_response_wait_ignored` event, opened the real workspace in headless Chrome, clicked Requests, and confirmed the pane showed `user-response-worker-live-browser-4`, the request question, `worker_agent`, and `Recorded`. Artifacts: `.temp/user-response-worker-live-browser-4/requests-dom.html`, `.temp/user-response-worker-live-browser-4/requests-text.json`, `.temp/user-response-worker-live-browser-4/requests-pane.png`.

## Residual Risk

The browser verification used the persisted shape produced by the WorkerAgent ignored-request path instead of waiting for a live provider to emit the hidden control. It verifies creation, persistence, event shape, active-session rendering, and browser-visible UI, but not provider compliance with the hidden control syntax.
