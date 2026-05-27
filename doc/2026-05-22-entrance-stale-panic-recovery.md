Behavior changed: idle resident services in persisted `panic` state can now self-heal when they have no session pending inputs, no service pending inputs, and no GoalManager pending work. This prevents Entrance-style standing communication sessions from becoming permanently undispatchable due to stale panic state after status-only or failed compaction loops.

Follow-up behavior changed: successful GoalManager compaction now explicitly clears the persisted GoalManager audit mirror to `all_clear`, while suppressed compaction remains `needs_compact` and failed compaction records `panic`. This keeps Entrance from repeatedly requesting compaction after the provider-side audit state has already recovered.

Files touched: `src/runtime/cli_service_adapter.py`, `src/runtime/goal_persist.py`, `tests/test_goal_manager_compact.py`.

Verification run:
- `python3 -m unittest tests.test_goal_manager_compact.GoalManagerCompactTests.test_handle_goal_manager_compact_request_is_suppressed_when_toggle_off tests.test_goal_manager_compact.GoalManagerCompactTests.test_handle_goal_manager_compact_request_calls_compactor_when_toggle_on tests.test_goal_manager_compact.GoalManagerCompactTests.test_handle_goal_manager_compact_request_keeps_noninteractive_helper_skip_as_checked -q`
- `python3 -m unittest tests.test_goal_manager_compact.GoalManagerCompactTests.test_maybe_clear_stale_idle_agent_panic_clears_idle_service_without_pending_work tests.test_goal_manager_compact.GoalManagerCompactTests.test_maybe_clear_stale_idle_agent_panic_keeps_panic_when_service_pending_work_exists -q`
- `python3 -m unittest tests.test_session_listing tests.test_goal_manager_compact -q`
- repaired the live resident session state and reconciled stale `waiting_on_children`
- pending/session/service audit state readback confirmed the resident service audit states are now `all_clear`

Remaining risk: the runtime still relies on persisted GoalManager summaries for some user-facing status surfaces, so stale descriptive text can outlive repaired machine state until a new review or explicit reconciliation updates it.
