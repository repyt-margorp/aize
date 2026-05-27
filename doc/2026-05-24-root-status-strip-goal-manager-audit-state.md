Behavior changed: root-page and session-summary audit visibility now prefer authoritative idle GoalManager `all_clear` state for active in-progress sessions even when no bound worker service is currently attached. This keeps the root status strip and session summaries from surfacing stale worker `panic` as the current audit state after GoalManager has already returned the session to a healthy resident idle state.

Files touched:
- `src/runtime/http_handler.py`
- `src/runtime/session_view.py`

Verification run:
- `python3 -m unittest tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_root_page_status_strip_prefers_newer_goal_manager_audit_state tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_root_page_renders_session_map_with_registered_unit_metadata tests.test_session_listing.SessionListingTests.test_session_summary_prefers_goal_manager_all_clear_over_stale_worker_panic tests.test_session_listing.SessionListingTests.test_session_summary_uses_persisted_queued_goal_manager_state tests.test_entrance_page.EntrancePageTests.test_entrance_page_renders_chat_polling_surface tests.test_entrance_page.EntrancePageTests.test_entrance_immediate_ack_does_not_claim_agent_activity_without_state tests.test_session_template.SessionTemplateLauncherTests.test_entrance_unit_provisions_code_based_interactive_skill -q`
- `python3 -m py_compile src/runtime/http_handler.py src/runtime/session_view.py`
- `git diff --check -- src/runtime/http_handler.py src/runtime/session_view.py`

Remaining risk:
- This change narrows the authoritative GoalManager preference to sessions whose goal is still actively `in_progress`; sessions outside that state still rely on the existing stronger/newer audit ordering rules.
