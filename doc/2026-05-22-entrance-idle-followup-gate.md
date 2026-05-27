Behavior changed:
Resident communication sessions no longer re-queue GoalManager review after a status-only turn when the batch contains no actionable work. System-only follow-up inputs like `goal_update`, `goal_feedback`, and `turn_completed` now leave the Entrance session cleanly idle unless there is actual routed work, a child-session signal, a resume directive, or spawned follow-up work to process.

Suppressed resident status turns now also advance the GoalManager review cursor using the same `turn_completed` timestamp written into session state. That prevents restart/compaction recovery from rediscovering those same resident reconciliation turns later as synthetic `unreviewed_turn_completed` work.

Status-only communication-session turns also no longer append a session-level `turn_completed` pending input at all. That removes the remaining evidence path logged as `service.post_turn_turn_completed_appended` for resident status/report turns while preserving `turn_completed` input appends for actionable user input, child-session signals, resume directives, and spawned follow-up work.

GoalManager runtime persistence now mirrors the same status/audit snapshot into the bound session service file as well as the reviewer service file. That keeps `goal_manager/state.json` and the user-facing `services/service-codex-001.json` snapshot aligned after a reviewer service like `service-codex-development-lineage-check-001` finishes an audit.

Communication sessions are now excluded from generic `active_in_progress_idle_reconcile` dispatch. Their active resident goal is maintained by explicit user prompts, child-session signals, resume directives, spawned work, and GoalManager review; empty idle UI refreshes should not dispatch a WorkerAgent turn every 30-60 seconds.

GoalManager audit completion now guards against an ownerless `in_progress` result. If a GoalManager review returns `progress_state=in_progress` with `audit_state=all_clear` but produces no agent directives, no child-session requests, and no user-response wait request, the runtime injects a fallback continuation directive back to the GoalManager itself instead of leaving the session at `goal_progress_state=in_progress` with no active GM or worker.

Files touched:
- `src/runtime/agent_service.py`
- `src/runtime/communication_goal.py`
- `src/runtime/goal_persist.py`
- `src/runtime/session_view.py`
- `tests/test_goal_manager_compact.py`
- `doc/2026-05-22-entrance-idle-followup-gate.md`

Verification run:
- `python3 -m py_compile src/runtime/agent_service.py src/runtime/goal_persist.py src/runtime/session_view.py tests/test_goal_manager_compact.py`
- `python3 -m unittest tests.test_goal_manager_compact.GoalManagerCompactTests.test_persist_goal_audit_completion_mirrors_state_into_bound_session_service tests.test_goal_manager_compact.GoalManagerCompactTests.test_persisted_goal_manager_runtime_state_refreshes_stale_bound_service_snapshot tests.test_goal_manager_compact.GoalManagerCompactTests.test_goal_manager_post_turn_does_not_enqueue_followup_for_its_own_turn tests.test_goal_manager_compact.GoalManagerCompactTests.test_goal_manager_post_turn_skips_empty_communication_goal_status_turns tests.test_goal_manager_compact.GoalManagerCompactTests.test_goal_manager_review_cursor_advances_for_suppressed_communication_status_turns tests.test_goal_manager_compact.GoalManagerCompactTests.test_session_turn_completed_input_skips_status_only_communication_turns`
- `python3 -m py_compile src/runtime/communication_goal.py src/runtime/cli_service_adapter.py src/runtime/http_handler.py src/runtime/agent_service.py tests/test_goal_manager_compact.py`
- `python3 -m unittest tests.test_goal_manager_compact.GoalManagerCompactTests.test_should_idle_goal_reconcile_skips_communication_sessions tests.test_goal_manager_compact.GoalManagerCompactTests.test_should_idle_goal_reconcile_keeps_standard_goal_sessions tests.test_goal_manager_compact.GoalManagerCompactTests.test_goal_manager_post_turn_does_not_enqueue_followup_for_its_own_turn tests.test_goal_manager_compact.GoalManagerCompactTests.test_goal_manager_post_turn_skips_empty_communication_goal_status_turns tests.test_goal_manager_compact.GoalManagerCompactTests.test_goal_manager_review_cursor_advances_for_suppressed_communication_status_turns tests.test_goal_manager_compact.GoalManagerCompactTests.test_session_turn_completed_input_skips_status_only_communication_turns`
- `python3 -m unittest tests.test_goal_manager_compact.GoalManagerCompactTests.test_in_progress_goal_without_followup_owner_falls_back_to_goal_manager tests.test_goal_manager_compact.GoalManagerCompactTests.test_in_progress_goal_with_user_wait_does_not_add_fallback_owner`

Remaining risk:
The idle reconcile gate is intentionally scoped to communication-agent sessions, and the new snapshot mirroring only affects GoalManager persistence. Non-communication follow-up flows were checked only through the standard idle reconcile unit case. The live resident session still needs a post-restart observation window to confirm that `runtime.active_goal_idle_reconcile` stops firing for Entrance.
