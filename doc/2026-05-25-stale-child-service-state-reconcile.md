Behavior changed:
- Added an authoritative stale-service reconciliation pass for session service snapshots so an idle GoalManager with no pending work clears stale non-preferred service records that were still showing `running` or `queued` after finite child work had already ended.
- The reconciliation also clears stale audit-only residue for those idle snapshots so goal-state and runtime-state visibility do not keep surfacing obsolete panic/running badges from superseded finite child services.
- Applied the reconciliation to the live AIzeDevelopment parent session state for `repyt/0ac1231110d2881f`, which reset stale records such as `service-codex-003`, `service-codex-004`, and `service-claude-006` to idle/all-clear without changing the active parent binding.

Files touched:
- `src/runtime/persistent_state_pkg/agent_audit.py`
- `src/runtime/persistent_state_pkg/__init__.py`
- `src/runtime/session_view.py`
- `tests/test_goal_manager_compact.py`

Verification run:
- `python3 -m unittest -q tests.test_goal_manager_compact.GoalManagerCompactTests.test_persisted_goal_manager_runtime_state_reconciles_stale_nonpreferred_service_snapshots tests.test_goal_manager_compact.GoalManagerCompactTests.test_load_session_audit_summary_ignores_reconciled_stale_nonpreferred_panic tests.test_goal_manager_compact.GoalManagerCompactTests.test_persisted_goal_manager_runtime_state_refreshes_stale_bound_service_snapshot tests.test_goal_manager_compact.GoalManagerCompactTests.test_persisted_goal_manager_runtime_state_exposes_queued_pending_work tests.test_goal_manager_compact.GoalManagerCompactTests.test_persisted_goal_manager_runtime_state_reads_state_file`
- Direct runtime-state check via a Python snippet against `.aize-state/sessions/repyt/0ac1231110d2881f` confirming `service-codex-003`, `service-codex-004`, and `service-claude-006` now persist as idle, with empty GoalManager pending work and `all_clear` audit state where an audit file exists.
- Direct session-summary check via `build_session_runtime_summary` confirming goal-state visibility, runtime-state visibility, routing/session lineage, and session permissions still read correctly for both `repyt/0ac1231110d2881f` and child audit session `repyt/98e3730b24680c36`.

Remaining risk:
- Older terminal failed worker records such as `service-codex-005` and `service-codex-007` still retain panic audit state because this change intentionally avoids rewriting terminal failure evidence. If those should also be normalized, that needs a separate policy decision.
