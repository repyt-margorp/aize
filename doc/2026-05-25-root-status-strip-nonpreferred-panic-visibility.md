Behavior changed: session audit summary resolution now keeps a newer noncanonical agent `panic` visible instead of letting an older authoritative idle GoalManager `all_clear` hide it. Canonical worker panics for active in-progress goal sessions still collapse to GoalManager `all_clear` when the resident review is intentionally authoritative.

Files touched:
- `src/runtime/persistent_state_pkg/agent_audit.py`
- `tests/test_goal_manager_compact.py`

Verification run:
- `python3 -m unittest -q tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_root_page_status_strip_uses_strongest_session_audit_state tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_root_page_status_strip_prefers_newer_goal_manager_audit_state tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_session_goal_save_resets_goal_manager_runtime_state_and_dispatches_review tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_message_goal_mode_resets_goal_manager_runtime_state tests.test_goal_manager_compact.GoalManagerCompactTests.test_load_session_audit_summary_prefers_authoritative_idle_goal_manager_all_clear tests.test_goal_manager_compact.GoalManagerCompactTests.test_load_session_audit_summary_keeps_newer_nonpreferred_panic_visible`

Remaining risk:
- The authoritative-idle override still treats canonical worker audit files as stale worker state for active resident goals; if a future flow wants canonical peer panics to remain user-visible, that distinction will need an explicit persisted source marker instead of service-id shape.
