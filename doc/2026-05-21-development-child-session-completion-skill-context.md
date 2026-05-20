Behavior changed: GoalManager audits now receive recorded session-skill rules, including finite/non-resident child-session conventions. This lets GoalManager treat explicit skill guidance such as "mark complete after implementation, verification, and reporting" as a completion rule instead of evaluating the child like a generic long-lived session.

Files touched: `src/runtime/goal_audit.py`, `tests/test_goal_manager_compact.py`.

Verification run:
- `python3 -m py_compile src/runtime/goal_audit.py`
- `python3 -m py_compile tests/test_goal_manager_compact.py`
- `python3 -m unittest tests.test_goal_manager_compact.GoalManagerCompactTests.test_build_goal_audit_prompt_mentions_multi_agent_turncompleted_review`
- `python3 -m unittest tests.test_goal_manager_compact.GoalManagerCompactTests.test_run_goal_audit_includes_session_skill_rules_in_prompt`
- `python3 -m unittest tests.test_goal_manager_compact.GoalManagerCompactTests.test_run_goal_audit_parses_two_axis_state`

Remaining risk: This improves GoalManager's explicit context, but completion still depends on audit evidence in the session log. Sessions interrupted before a `turn.completed` record is appended will still resume as unfinished work, which is correct for genuinely interrupted turns.
