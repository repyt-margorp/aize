# User Response Request UI Surface

## Behavior Changed

Worker-originated user response request records now surface in the user-visible Requests pane from both live realtime events and the active-session fallback state. Recorded requests show the request id, question, background, timeout policy, source service, and worker role. They remain non-blocking records because only GoalManager may start an active user wait.

## Cause

Request creation and persistence were working through `record_session_user_response_request()`, and WorkerAgent attempts were correctly converted to `service.user_response_wait_ignored` records. The failure was in Entrance rendering and event propagation: realtime request events were not fully patching the Requests-pane summary cache, and the active-session fallback path could render the request without the worker/source metadata when the `/sessions` request history was not available yet.

## Files Touched

- `src/runtime/html_renderer.py`
- `src/runtime/http_handler.py`
- `src/runtime/session_view.py`
- `tests/test_entrance_page.py`

## Verification

- `python3 -m py_compile src/runtime/html_renderer.py src/runtime/http_handler.py src/runtime/session_view.py`
- `PYTHONPATH=./src python3 -m unittest tests.test_goal_manager_compact.GoalManagerCompactTests.test_record_session_user_response_request_sets_visible_summary_fields tests.test_goal_manager_compact.GoalManagerCompactTests.test_run_goal_audit_parses_user_response_request tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_cross_user_session_view_lists_session_owner_requests_for_owned_scope tests.test_entrance_page.EntrancePageTests.test_main_page_renders_latest_first_workspace_header_and_fixed_composer tests.test_entrance_page.EntrancePageTests.test_entrance_page_renders_chat_polling_surface -v`
- Restarted the runtime with `./restart_aize_unit.sh`.
- Browser verification against live HttpBridge at `https://127.0.0.1:64123`: seeded a worker-originated recorded request through the same persistent shape used by WorkerAgent ignored requests, opened the live UI in headless Chrome, clicked Requests, and confirmed the pane rendered `user-response-worker-browser-e2e`, the question text, and `worker_agent`. Artifacts: `.temp/user-response-worker-browser-final/requests-dom.html`, `.temp/user-response-worker-browser-final/requests-text.txt`, `.temp/user-response-worker-browser-final/requests-pane.png`.

## Residual Risk

The browser run seeded the persisted WorkerAgent request record directly rather than waiting for a live provider to emit the hidden control syntax. It verifies creation shape, persistence, UI routing, active-session rendering, and browser visibility, but provider compliance with the hidden request syntax remains dependent on model output.
