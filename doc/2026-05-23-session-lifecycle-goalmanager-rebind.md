## Session lifecycle GoalManager rebind

User-visible behavior changed: active in-progress sessions now rebind GoalManager ownership both when HttpBridge releases a stale or unavailable non-GoalManager service binding and when a live non-GoalManager turn settles with no remaining active owner. The runtime now queues a GoalManager lifecycle review instead of leaving the session `in_progress` with neither a replying agent nor a queued/running reviewer.

The persisted GoalManager runtime state is written as `queued`, and session summaries now fold that persisted reviewer state back into runtime visibility so SessionMap, runtime summaries, provider counts, and status badges can see the session as having an active reviewer instead of appearing idle with no owner.

Files touched: `src/runtime/agent_service.py`, `src/runtime/session_view.py`, `src/runtime/session_lifecycle.py`, `src/runtime/cli_service_adapter.py`, `tests/test_goal_manager_compact.py`, `tests/test_session_listing.py`, `tests/test_http_handler_goal_save.py`.

Verification run:
- `python3 -m py_compile src/runtime/agent_service.py src/runtime/session_view.py tests/test_goal_manager_compact.py tests/test_session_listing.py`
- `python3 -m unittest tests.test_goal_manager_compact.GoalManagerCompactTests.test_non_goal_manager_turn_completion_requeues_goal_manager_when_session_would_be_ownerless tests.test_goal_manager_compact.GoalManagerCompactTests.test_non_goal_manager_turn_completion_preserves_waiting_on_children_without_requeue tests.test_goal_manager_compact.GoalManagerCompactTests.test_lifecycle_owner_loss_queues_goal_manager_review tests.test_goal_manager_compact.GoalManagerCompactTests.test_lifecycle_owner_loss_uses_existing_goal_manager -q`
- `python3 -m unittest tests.test_session_listing.SessionListingTests.test_session_summary_uses_persisted_queued_goal_manager_state tests.test_session_listing.SessionListingTests.test_session_summary_prefers_goal_manager_all_clear_over_stale_worker_panic -q`
- `python3 -m unittest tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_root_page_idle_reconcile_skips_sessions_with_goal_manager_already_queued tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_root_page_renders_session_map_with_registered_unit_metadata -q`
- `python3 -m unittest tests.test_entrance_page.EntrancePageTests.test_entrance_page_renders_chat_polling_surface tests.test_entrance_page.EntrancePageTests.test_entrance_immediate_ack_does_not_claim_agent_activity_without_state -q`
- `python3 -m unittest tests.test_goal_manager_compact.GoalManagerCompactTests.test_lifecycle_owner_loss_queues_goal_manager_review tests.test_goal_manager_compact.GoalManagerCompactTests.test_lifecycle_owner_loss_uses_existing_goal_manager tests.test_goal_manager_compact.GoalManagerCompactTests.test_release_nonrunnable_session_services_releases_stopped_bound_worker tests.test_goal_manager_compact.GoalManagerCompactTests.test_release_nonrunnable_session_services_keeps_running_in_progress_binding tests.test_goal_manager_compact.GoalManagerCompactTests.test_release_nonrunnable_session_services_keeps_parent_bound_worker_while_child_runs -q`
- `python3 -m unittest tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_root_page_idle_reconcile_skips_empty_communication_sessions tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_root_page_idle_reconcile_skips_sessions_with_goal_manager_already_queued -q`
- `python3 -m unittest tests.test_session_listing -q`
- `python3 -m unittest tests.test_entrance_page -q`
- `python3 -m py_compile src/runtime/session_lifecycle.py src/runtime/cli_service_adapter.py tests/test_goal_manager_compact.py tests/test_http_handler_goal_save.py`
- `git diff --check`

Additional broad checks attempted:
- `python3 -m unittest tests.test_session_listing -q` passed.
- `python3 -m unittest tests.test_entrance_page -q` passed.
- `python3 -m unittest tests.test_goal_manager_compact -q` still fails in existing Codex provider fake-process tests because the local `FakeProc` lacks `stdin`.
- `python3 -m unittest tests.test_http_handler_goal_save -q` still fails one existing audit-precedence expectation in `test_root_page_status_strip_prefers_newer_goal_manager_audit_state`.

Adjacent invariants checked: goal state visibility through active/in-progress guards, runtime state visibility through queued GoalManager state and persisted session-summary fallback, routing and session lineage through the waiting-on-children skip plus Entrance page tests, local delivery through the generated dispatch message and GoalManager agent profile, and user-facing status badges through the existing session-listing and HttpBridge page tests. The communication-session idle reconcile gate remains explicit: empty communication sessions are not requeued by the generic idle loop, and the new handoff only runs when a non-GoalManager turn actually settles.

Remaining risk: if no running LLM service exists in the GoalManager priority pools, the lifecycle event is logged with `no_available_goal_manager_worker`; the runtime still cannot assign a reviewer until a provider service is available.
