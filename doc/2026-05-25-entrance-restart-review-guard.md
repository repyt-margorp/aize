Behavior changed: restart recovery no longer auto-enqueues a synthetic GoalManager review for an idle Entrance-style communication session that is both `communication_agent_enabled=true` and `goal_completion_policy=continuous` when there is no unfinished turn, actionable pending work, due auto-resume, stale GoalManager runtime, or unreviewed `TurnCompleted`.

Files touched: `src/runtime/compaction.py`, `tests/test_goal_manager_compact.py`.

Verification run:
- `python3 -m unittest tests.test_goal_manager_compact.GoalManagerCompactTests.test_maybe_resume_after_restart_skips_idle_continuous_communication_goal tests.test_goal_manager_compact.GoalManagerCompactTests.test_maybe_resume_after_restart_routes_provider_scoped_idle_in_progress_to_goal_manager tests.test_goal_manager_compact.GoalManagerCompactTests.test_maybe_resume_after_restart_routes_unreviewed_turn_to_goal_manager tests.test_goal_manager_compact.GoalManagerCompactTests.test_maybe_resume_after_restart_routes_active_in_progress_even_after_terminal_goal_audit -q`
- `python3 -m py_compile src/runtime/compaction.py tests/test_goal_manager_compact.py`
- `git diff --check -- src/runtime/compaction.py tests/test_goal_manager_compact.py`

Remaining risk: this guard is intentionally narrow. Restart recovery still routes continuous communication sessions through GoalManager when there is real pending evidence to inspect, such as unreviewed completions, persisted GoalManager work, stale runtime state, or explicit auto-resume work.
