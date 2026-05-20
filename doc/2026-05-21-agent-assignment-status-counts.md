Behavior changed: SessionMap provider status now derives assigned-agent totals from the persisted session roster, not only from currently replying/reviewing workers. The provider pills and summary text now show `assigned` separately from `busy`, with GoalManager reviewer counts visible in the same status line.

Files touched: `src/runtime/session_view.py`, `src/runtime/html_renderer.py`, `tests/test_goal_manager_compact.py`.

Verification run: pending in this session after the code patch; targeted unit tests and SessionMap UI verification are the next step.

Remaining risk: any legacy assignment path that fails to persist `welcomed_agents` or the bound service ID can still undercount until that persistence path is corrected.
