Behavior changed: continuous communication sessions such as Entrance no longer auto-dispatch `active_in_progress_idle_reconcile` while they are legitimately idle with no active worker or GoalManager turn. This prevents empty self-turns from being misclassified as stuck/no-op progress and escalating to GoalManager panic.

Files touched: `src/runtime/communication_goal.py`, `src/runtime/cli_service_adapter.py`, `src/runtime/http_handler.py`, `tests/test_goal_manager_compact.py`.

Verification run: `python3 -m pytest tests/test_goal_manager_compact.py -q`.

Remaining risk: this fix is intentionally narrow to communication-mode sessions with `communication_agent_enabled=true`; other active-goal session types still use idle reconcile behavior unchanged.
