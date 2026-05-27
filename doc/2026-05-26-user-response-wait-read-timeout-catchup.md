# User Response Wait Read Timeout Catch-Up

- User-visible behavior changed: expired `user_response_wait_active` state is now cleared when session records are read through `get_session_settings()` or session listing paths, so stale timed-out Requests entries do not stay visible indefinitely if the background timeout watcher misses a window during restart/recovery churn.
- Files touched: `src/runtime/persistent_state_pkg/conversation.py`, `tests/test_goal_manager_compact.py`, `tests/test_session_listing.py`.
- Verification run:
  - `PYTHONPATH=./src python3 -m unittest tests.test_goal_manager_compact.GoalManagerCompactTests.test_consume_session_due_user_response_wait_clears_wait_state tests.test_goal_manager_compact.GoalManagerCompactTests.test_get_session_settings_clears_due_user_response_wait_state tests.test_session_listing.SessionListingTests.test_list_sessions_clears_due_user_response_wait_state -v`
  - Live check: `get_session_settings(..., session_id='f4f389a5c7233918')` cleared the stale expired wait and marked its last request `timed_out`.
  - Live monitor check: `python3 -m runtime.system_monitor --runtime-root ./.aize-runtime --json | jq '.counts, [.findings[] | select(.unresolved_user_input==true)]'`
- Result: the stale `Requests Repyt Active Browser Verify` wait no longer appears as unresolved; the only remaining unresolved wait after the fix is `Requests Repyt Active Browser Verify 2`, whose `until_at` is still in the future and is therefore legitimate.
- Remaining risk: this is opportunistic catch-up on read, not a replacement for the background timeout watcher. Sessions that are never read after a missed watcher window can still remain stale on disk until a read path touches them.
