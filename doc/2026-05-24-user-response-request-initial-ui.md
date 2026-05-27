# User Response Request Initial UI

## Behavior Changed

UserResponseRequest records now survive the server-rendered HTTPBridge initial page model. A GoalManager-created request that originated from a worker blocker is available to the Requests pane, session metadata, and status strip without relying on a later `/sessions` refresh to recover missing request history.

## Cause

Request creation, persistence, routing, and GoalManager parsing were already functioning: GoalManager emits `user_response_request`, `AgentService` persists it with `update_session_user_response_wait()`, and `/sessions` exposes the request history. The failing path was Entrance/HTTPBridge rendering: `_initial_session_summaries_for_view()` copied only the session-level wait snapshot and omitted `user_response_wait_requests`, timeout seconds, effective timeout seconds, and deadline. The page could therefore load with a persisted request but without the complete request item data needed by the user-visible request board.

HTTPBridge restart verification also exposed a startup ordering bug: stale-session reconciliation could call `send_router_control` before that helper was defined. The call is now made after the helper exists.

## Files Touched

- `src/runtime/http_handler.py`
- `src/runtime/cli_service_adapter.py`
- `tests/test_http_handler_goal_save.py`

## Verification

- `python3 -m py_compile src/runtime/cli_service_adapter.py src/runtime/http_handler.py`
- `python3 -m unittest tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_root_page_renders_session_map_with_registered_unit_metadata tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_cross_user_session_view_lists_session_owner_requests_for_owned_scope tests.test_session_listing.SessionListingTests.test_session_summary_exposes_user_response_request_history tests.test_goal_manager_compact.GoalManagerCompactTests.test_run_goal_audit_parses_user_response_request -v`
- Restarted with `./restart_aize_unit.sh`; live health returned `200 OK` on the active HTTPS HttpBridge.
- Browser-verified with headless Chrome against the live HTTPS HttpBridge. A synthetic worker-blocker/GoalManager wait record rendered `Waiting for User`, request id `user-response-browser-e2e`, and the question text in the user-visible UI. Artifacts: `.temp/user-response-browser-e2e/requests-pane.png`, `.temp/user-response-browser-e2e/requests-dom.html`.

## Residual Risk

The browser verification used a synthetic persisted worker-blocker plus GoalManager wait record to avoid sending a real prompt that would require human input. It covers the live HTTPBridge rendering path and the persisted shape produced by GoalManager, but not a fresh full LLM turn that independently decides to ask the user.
