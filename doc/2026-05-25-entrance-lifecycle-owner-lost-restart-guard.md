Behavior changed: `enqueue_goal_manager_lifecycle_review` no longer enqueues a `lifecycle_owner_lost` GoalManager review when a continuous communication session (`communication_agent_enabled=true` + `goal_completion_policy=continuous`) has its ephemeral interactive worker released as part of restart bookkeeping (reason prefix `released_nonrunnable_session_service:`). Any stale `lifecycle_owner_lost` items left in the GoalManager state file or pending input log from the pre-guard restart loop are purged on the next call so the queued review item finally clears.

User-visible effect: Entrance-style sessions no longer perpetually carry a `pending_work_items=[{kind: lifecycle_owner_lost, reason: released_nonrunnable_session_service:service_missing}]` entry across restarts. The interactive worker still respawns on the next user input as it always did; the GoalManager is no longer asked to review a non-event.

Files touched:
- `src/runtime/communication_goal.py` — added `is_continuous_communication_session(session_settings)` helper.
- `src/runtime/session_lifecycle.py` — added `_is_released_nonrunnable_reason` and `_purge_continuous_communication_restart_owner_lost_state` helpers, then gated `enqueue_goal_manager_lifecycle_review` so the continuous-communication restart-bookkeeping path short-circuits and cleans up stale matching entries.
- `tests/test_goal_manager_compact.py` — added `test_lifecycle_owner_loss_skips_continuous_communication_ephemeral_worker` (covers the new guard plus stale-entry purge) and `test_lifecycle_owner_loss_still_queues_for_standard_session_after_restart` (regression for the unchanged standard-session path).

Verification:
- `python3 -m py_compile src/runtime/communication_goal.py src/runtime/session_lifecycle.py tests/test_goal_manager_compact.py`
- `python3 -m unittest tests.test_goal_manager_compact.GoalManagerCompactTests.test_lifecycle_owner_loss_skips_continuous_communication_ephemeral_worker tests.test_goal_manager_compact.GoalManagerCompactTests.test_lifecycle_owner_loss_still_queues_for_standard_session_after_restart tests.test_goal_manager_compact.GoalManagerCompactTests.test_lifecycle_owner_loss_queues_goal_manager_review tests.test_goal_manager_compact.GoalManagerCompactTests.test_lifecycle_owner_loss_uses_existing_goal_manager`
- `python3 -m unittest tests.test_goal_manager_compact tests.test_session_listing tests.test_http_handler_goal_save tests.test_entrance_page` — 300 tests OK.

Remaining risk: the guard is narrow. The non-GoalManager-turn-completion path in `agent_service._maybe_enqueue_in_progress_goal_lifecycle_review` still enqueues lifecycle reviews via the same function but with non-`released_nonrunnable_session_service:` reasons, so legitimate owner-loss handoffs (the case the 2026-05-23 directive asked for) continue to dispatch to the GoalManager unchanged. Live verification of the runtime loop break depends on a restart that picks up the new code, which has not been performed in this turn.
