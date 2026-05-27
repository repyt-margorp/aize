# Worker user response request UI

Behavior changed: worker-originated `<aize_user_response_wait>` controls remain non-authoritative and are recorded as `service.user_response_wait_ignored`, but they now surface in user-visible UI instead of only persisting in session state. Entrance chat renders the recorded request text, Entrance status badges refresh from user-response-wait events, and the main workspace status strip updates immediately for recorded worker-originated requests.

Files touched:
- `src/runtime/html_renderer.py`
- `src/runtime/agent_service.py`
- `tests/test_entrance_page.py`

Verification run:
- `python3 -m unittest tests.test_entrance_page.EntrancePageTests.test_entrance_page_renders_chat_polling_surface tests.test_entrance_page.EntrancePageTests.test_main_page_renders_latest_first_workspace_header_and_fixed_composer -v`
- Browser verification over live HttpBridge with a persisted worker-originated request event, confirming the UI DOM included the session label, generated request id, recorded status, request question, and Requests pane markers. Artifacts: `.temp/user-response-browser-e2e/proxy-dom.html`, `.temp/user-response-browser-e2e/proxy-screenshot.png`.

Follow-up fix: `AgentService` now emits the normalized persisted request id, generated timestamp, effective timeout, and deadline on `service.user_response_wait_ignored`. Previously a worker request without an explicit request id was persisted with a generated id, but the realtime event still carried an empty id, so the UI could not upsert the request record until a later `/sessions` refresh.

Remaining risk: worker-originated requests intentionally do not start an active wait; only GoalManager-originated requests can create a blocking `Waiting for User` state.
