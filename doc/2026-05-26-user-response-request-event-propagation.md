# User Response Request Event Propagation

## Behavior Changed

Worker-originated user response request records now retain their generated timestamp and deadline in the live Workspace Requests pane immediately from the `service.user_response_wait_ignored` event. The Requests pane also stays live after it has loaded its own `/sessions` cache: realtime request events now patch both the main workspace summaries and the request-pane summary cache. These requests remain non-authoritative records: they surface to the user, but they do not start an active blocking wait because only GoalManager may request direct user feedback.

## Cause

Request creation and persistence were already functioning through `record_session_user_response_request()`, and `/sessions` exposed the persisted request history. The remaining gap was event propagation in the Entrance/Workspace renderer. The ignored worker-request event path updated status, prompt, reason, and timeout fields, but dropped `event.generated_at` and `event.until_at` when patching the in-memory summary and request history. A second live-render gap remained after the Requests pane had refreshed: `renderUserRequests()` preferred `requestWorkspaceSummaries`, but realtime events only patched `workspaceSummaries`, so a new worker-originated request could be hidden until the next request-pane refresh. The fix keeps both caches in sync.

## Files Touched

- `src/runtime/html_renderer.py`
- `tests/test_entrance_page.py`

## Verification

- `python3 -m py_compile src/runtime/html_renderer.py`
- `python3 -m unittest tests.test_entrance_page.EntrancePageTests.test_main_page_renders_latest_first_workspace_header_and_fixed_composer tests.test_goal_manager_compact.GoalManagerCompactTests.test_record_session_user_response_request_sets_visible_summary_fields tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_cross_user_session_view_lists_session_owner_requests_for_owned_scope -v`
- `python3 -m unittest tests.test_goal_manager_compact tests.test_http_handler_goal_save tests.test_entrance_page`
- Browser verification against isolated HTTPBridge runtime on `http://127.0.0.1:32863`: seeded a worker-originated recorded request, opened the real UI in headless Chrome through CDP, clicked Requests, and confirmed the DOM contained request id `user-response-worker-browser-e2e`, the question text, `worker_agent`, recorded status, source service, and timeout policy. Artifacts: `.temp/user-response-request-e2e/browser/requests-dom.html`, `.temp/user-response-request-e2e/browser/requests-text.txt`, `.temp/user-response-request-e2e/browser/requests-pane.png`.
- `PYTHONPATH=./src python3 -m py_compile src/runtime/html_renderer.py`
- `PYTHONPATH=./src python3 -m unittest tests.test_entrance_page.EntrancePageTests.test_main_page_renders_latest_first_workspace_header_and_fixed_composer tests.test_goal_manager_compact.GoalManagerCompactTests.test_record_session_user_response_request_sets_visible_summary_fields tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_cross_user_session_view_lists_session_owner_requests_for_owned_scope -v`
- Browser verification with the live HTTPS HttpBridge page model and a persisted worker-originated request `user-response-browser-cache-e2e`: fetched the authenticated live page from `https://127.0.0.1:64123`, served the saved page with a `/sessions` response built from the actual persisted session record, clicked Requests in headless Chrome, and confirmed the rendered pane contained the request id, question text, `worker_agent`, recorded status, and source service. Artifacts: `.temp/user-response-cache-browser/proxy-dump-dom.html`, `.temp/user-response-cache-browser/proxy-screenshot.png`, `.temp/user-response-cache-browser/sessions.json`.

## Residual Risk

The browser run used a seeded persisted worker-originated request to avoid sending a live prompt that would require a real user answer. It covers the persistence shape produced by the worker request path and the real HTTPBridge UI rendering path. A live LLM-generated request still depends on the provider emitting the expected hidden control syntax.
