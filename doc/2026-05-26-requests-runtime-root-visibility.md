# Requests Runtime Root Visibility

Behavior investigated: pressing the HTTPBridge Requests button should surface a human-facing
`UserResponseRequest` for user `repyt` in Session UI.

Cause found: the reproduced hidden-request failure came from creating synthetic request/auth
state with `./.aize-state` as the runtime root. For the canonical runtime, AIze runs
HTTPBridge with `./.aize-runtime` as `runtime_root`; durable state is then resolved beside it
under `./.aize-state`. Passing `./.aize-state` directly resolves durable state under
`./.aize-state/.aize-state`, so the request object exists but the live HTTPBridge process cannot
see it.

Files touched during this investigation:
- `doc/2026-05-26-requests-runtime-root-visibility.md`
- Scratch verifier files under `./.temp/`

Verification:
- Created a repyt-owned request through the live runtime root.
- Confirmed the request persisted under the canonical session state and carried
  `source_service_id=service-codex-requests-debug-001` and `requested_by_role=goal_manager`.
- Appended the `service.user_response_wait_started` event to the session timeline.
- Opened live HTTPBridge in headless Chrome, clicked the real Requests button, and confirmed the
  rendered pane showed the request id, question text, `Waiting for User`, the source service, and
  one actively waiting user request.
- Ran targeted tests:
  `PYTHONPATH=./src python3 -m unittest tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_cross_user_session_view_lists_session_owner_requests_for_owned_scope tests.test_entrance_page.EntrancePageTests.test_main_page_renders_latest_first_workspace_header_and_fixed_composer -v`

Residual risk: broad `/sessions` probes can still be slow on large runtime state. The real browser
Requests flow rendered the repyt human-facing request successfully, but list-view latency remains a
separate responsiveness risk.
