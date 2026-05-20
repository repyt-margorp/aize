Busy provider counts could show `Codex Busy 0` while a session still had queued GoalManager review work in persisted state. The missing edge was that `goal_manager_state=queued` already counted in `build_worker_count_summary`, but downstream runtime-status rendering still collapsed it to idle.

Changed files:
- `src/runtime/status_gateway.py`
- `src/runtime/http_handler.py`
- `src/runtime/html_renderer.py`
- `tests/test_goal_manager_compact.py`

Behavior change:
- queued GoalManager pending work now keeps runtime execution state at `running` for session summaries and Entrance rendering, so provider busy totals stay aligned with queued review work.
- goal state visibility is unchanged: the UI still exposes `goal_manager_state=queued`; only runtime/busy visibility now treats that state as active work.

Verification:
- `python3 -m unittest tests.test_goal_manager_compact tests.test_entrance_page -q`

Remaining risk:
- This intentionally treats queued GoalManager review work as runtime-busy. If a future UI needs to distinguish queued review from actively executing review, that should be added as a separate surface instead of folding it back into idle.
