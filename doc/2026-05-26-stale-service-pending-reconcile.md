Stale nonpreferred service reconciliation now purges orphaned agent-scoped pending queue files when the session goal manager is already in an authoritative idle/all-clear state. This prevents old `interactive_agent` queue files from surviving restart and continuing to appear as live pending work after the session has already settled.

Files touched: `src/runtime/persistent_state_pkg/agent_audit.py`, `tests/test_goal_manager_compact.py`.

Verification run: `PYTHONPATH=./src python3 -m unittest tests.test_goal_manager_compact.GoalManagerCompactTests.test_load_session_audit_summary_clears_agent_scoped_pending_for_reconciled_stale_service tests.test_goal_manager_compact.GoalManagerCompactTests.test_persisted_goal_manager_runtime_state_reconciles_stale_nonpreferred_service_snapshots tests.test_goal_manager_compact.GoalManagerCompactTests.test_load_session_audit_summary_ignores_reconciled_stale_nonpreferred_panic -v`

Residual risk: the purge is scoped to stale nonpreferred services during authoritative goal-manager reconciliation; if a future workflow intentionally keeps pending inputs on a nonpreferred service while the session-level goal manager is already idle/all-clear, that workflow would need an explicit durable signal instead of relying on the orphaned queue file alone.
