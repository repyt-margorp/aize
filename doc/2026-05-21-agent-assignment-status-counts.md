Behavior changed: SessionMap provider status now derives assigned-agent totals from the persisted session roster, not only from currently replying/reviewing workers. The provider pills and summary text now show `assigned` separately from `executing`, with GoalManager reviewer counts visible in the same status line. This prevents the top Status/SessionDAG wording from implying `Busy 0` means `0 assigned` when active sessions still hold assigned agents.

Files touched: `src/runtime/session_view.py`, `src/runtime/html_renderer.py`, `tests/test_goal_manager_compact.py`.

Verification run: targeted unit tests for provider aggregation and page-source assertions, authenticated live `/sessions?scope=owned` on the running HttpBridge, and authenticated DOM loading of the Workspace root.

Remaining risk: any legacy assignment path that fails to persist `welcomed_agents` or the bound service ID can still undercount until that persistence path is corrected. Invariants checked here: runtime state visibility stayed aligned with live `/sessions` worker counts, goal-state/session-card visibility remained intact, no routing/session-lineage logic changed, and no permissions logic changed beyond reusing existing authenticated read paths for verification.
