Behavior changed: when GoalManager records a later `goal_manager_last_reviewed_turn_completed_at`, it now drops already reviewed `turn_completed` work items from `goal_manager/state.json`. If that was the only queued work, the GoalManager runtime state returns to `idle` and stale requeue markers are cleared.

Files touched: `src/runtime/persistent_state_pkg/conversation.py`, `tests/test_goal_manager_compact.py`.

Verification run: `python3 -m unittest tests.test_goal_manager_compact.GoalManagerCompactTests.test_update_goal_manager_review_cursor_clears_reviewed_pending_work -q`; `python3 -m unittest tests.test_session_listing -q`.

Remaining risk: only `turn_completed` pending items are auto-consumed by the review cursor reconciliation. Other pending work kinds still require their normal explicit completion paths.
