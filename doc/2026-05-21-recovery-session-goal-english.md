Recovery sessions now receive an English goal prompt instead of a Japanese one so panic-handling instructions are consistent with the intended provider-facing workflow.

Files touched: `src/runtime/panic_recovery.py`, `tests/test_goal_manager_compact.py`.

Verification run: `python3 -m unittest tests.test_goal_manager_compact.GoalManagerCompactTests.test_panic_recovery_goal_text_is_english`

Remaining risk: this change updates the goal text generator only; any previously created recovery sessions keep their already-persisted prompt text.
