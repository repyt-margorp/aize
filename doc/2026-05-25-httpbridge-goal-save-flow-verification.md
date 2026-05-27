Verified the HTTPBridge goal-save coverage around the direct `/session/goal` handler path in `tests/test_http_handler_goal_save.py`.

Behavior verified:
- Saving a goal through the HTTPBridge goal endpoint updates the stored goal text.
- The save flow clears stale GoalManager runtime state and stuck agent audit state.
- HTTPBridge re-dispatches GoalManager review with `reason="goal_saved"` and the prior goal context.
- The prompt-submission goal path still resets GoalManager runtime state with `reason="goal_updated"`.

Verification run:
- `python3 -m unittest tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_session_goal_save_resets_goal_manager_runtime_state_and_dispatches_review`
- `python3 -m unittest tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_message_goal_mode_resets_goal_manager_runtime_state`
- `python3 -m unittest tests.test_http_handler_goal_save`

Remaining risk:
- No additional failure was reproduced in `tests.test_http_handler_goal_save` during this verification pass.
