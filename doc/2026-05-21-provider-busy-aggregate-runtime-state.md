Provider busy aggregate now carries lightweight `agent_contacts` in session runtime summaries so the global SessionDAG/Status provider totals can keep counting assigned agents and GoalManager reviewers from live session state without depending on full talk payloads.

Persisted GoalManager runtime state now exposes queued pending work items. A queued pending GoalManager review, such as a restarted `turn_completed` review item, is treated as runtime-visible work: it contributes one provider `busy` reviewing turn, one assigned slot, and one GoalManager reviewer while preserving the existing completed/waiting goal progress states.

Files touched: `src/runtime/session_view.py`, `src/runtime/http_handler.py`, `src/runtime/http_dispatch.py`, `tests/test_goal_manager_compact.py`, `tests/test_entrance_page.py`, `tests/test_http_dispatch.py`.

Verification run:
- `python3 -m unittest tests.test_goal_manager_compact.GoalManagerCompactTests.test_persisted_goal_manager_runtime_state_reads_state_file tests.test_goal_manager_compact.GoalManagerCompactTests.test_persisted_goal_manager_runtime_state_exposes_queued_pending_work tests.test_goal_manager_compact.GoalManagerCompactTests.test_persisted_goal_manager_runtime_state_backfills_service_state_snapshot tests.test_goal_manager_compact.GoalManagerCompactTests.test_build_worker_count_summary_counts_running_replying_and_reviewing tests.test_goal_manager_compact.GoalManagerCompactTests.test_build_worker_count_summary_counts_queued_goal_manager_work_as_busy tests.test_goal_manager_compact.GoalManagerCompactTests.test_normalize_runtime_execution_state_treats_queued_goal_manager_as_running`
- `python3 -m unittest tests.test_session_listing`
- `python3 -m unittest tests.test_entrance_page.EntrancePageTests.test_main_page_renders_latest_first_workspace_header_and_fixed_composer tests.test_entrance_page.EntrancePageTests.test_communication_prompt_dispatch_no_longer_uses_worker_text_heuristic tests.test_entrance_page.EntrancePageTests.test_entrance_status_events_refresh_immediately_and_state_poll_converges`
- `python3 -m unittest tests.test_http_dispatch tests.test_entrance_page tests.test_goal_manager_compact`
- `python3 -m py_compile src/runtime/session_view.py src/runtime/http_handler.py src/runtime/cli_service_adapter.py src/runtime/status_gateway.py tests/test_goal_manager_compact.py tests/test_entrance_page.py`

`python3 -m pytest ...` was not available in this runtime because `pytest` is not installed.

Invariant families checked: runtime state visibility for queued pending review work, goal state visibility for completed/waiting GoalManager state preservation, routing/session lineage through the Entrance dispatch tests, and permissions by inspection because no permission paths were touched.

Remaining risk: other summary producers outside these runtime paths would still need to include `agent_contacts` and `goal_manager_pending_work_items` if they start feeding `build_worker_count_summary` directly.
