Continuous Entrance-style communication sessions no longer treat restart-resume control envelopes as actionable work when deciding whether to append a fresh session-level `turn_completed` marker after a reply.

Behavior changed
- `src/runtime/agent_service.py` now classifies post-turn actionable input so `restart_resume` and `scheduled_resume` still count for normal goal sessions, but not for `communication_agent_enabled=true` sessions unless the turn also handled real work such as user input, worker results, or child-goal requests.
- This prevents status-only restart health-check replies from creating another GoalManager review cycle in continuous resident communication sessions.

Files touched
- `src/runtime/agent_service.py`
- `tests/test_goal_manager_compact.py`
- `doc/2026-05-25-entrance-restart-turn-completed-guard.md`

Verification
- `python3 -m unittest tests.test_goal_manager_compact.GoalManagerCompactTests.test_actionable_post_turn_input_present_ignores_restart_resume_for_communication_sessions tests.test_goal_manager_compact.GoalManagerCompactTests.test_session_turn_completed_input_skips_status_only_communication_turns tests.test_goal_manager_compact.GoalManagerCompactTests.test_maybe_resume_after_restart_skips_idle_continuous_communication_goal -q`
- `python3 -m py_compile src/runtime/agent_service.py tests/test_goal_manager_compact.py`

Remaining risk
- This narrows post-turn churn for communication sessions after restart, but it does not retroactively clear stale pending review entries already written in existing live session state.
