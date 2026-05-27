# Requests Owned Session Scope

Behavior changed: the HTTPBridge Requests pane now refreshes request records from the active session owner scope even when the surrounding SessionMap is in a superuser/global view. Pressing Requests no longer carries `scope=all` into the request refresh query; it sends the active `session_id` and display window only, letting the server resolve the owner-scoped session list for that active session.

Cause: user response request creation and persistence were intact for `repyt/0ac1231110d2881f`, including `user-response-6ed5721f0794`, request history, GoalManager source metadata, timeout state, and the human-facing prompt text. The UI failure was scoped to the client refresh path: `requestDisplayQuery()` inherited the current SessionMap scope, so a global/superuser view could refresh the Requests pane through the Root-heavy all-session listing instead of the active repyt session owner view.

Files touched:
- `src/runtime/html_renderer.py`
- `tests/test_entrance_page.py`
- `doc/2026-05-26-requests-owned-session-scope.md`

Verification:
- Restarted the Unit runtime with `./restart_aize_unit.sh`; health returned 200 for `service-http-001`.
- `PYTHONPATH=./src python3 -m unittest tests.test_entrance_page.EntrancePageTests.test_main_page_renders_latest_first_workspace_header_and_fixed_composer tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_cross_user_session_view_lists_session_owner_requests_for_owned_scope -v`
- Browser verified the live HTTPS HTTPBridge with headless Chrome as `repyt`, opened `session_id=0ac1231110d2881f`, clicked Requests, and confirmed `user-response-6ed5721f0794` plus the prompt `Provide the next concrete AIze development task...` were visible. Artifacts: `.temp/requests-owned-browser-live/requests-pane.png`, `.temp/requests-owned-browser-live/requests-dom.html`, `.temp/requests-owned-browser-live/requests-text.txt`.

Residual risk: the browser verification used an existing timed-out GoalManager request record rather than forcing a fresh live human wait. It covers the same persisted request history and Requests button rendering path, but a live newly-created wait still depends on GoalManager emitting the expected request event.
