# Agent Capacity and Session Counts

## Behavior

- Default local provider pools now start 10 Codex, 10 Claude, and 10 Gemini agent processes unless overridden by the existing pool size environment variables.
- HTTPBridge session cards no longer render provider slot markers as user-facing session meaning.
- Provider summary text uses process counts for pool capacity and keeps slot labels out of the UI.
- Session cards now show aggregate session participation counts:
  - `Agents N` for assigned non-GoalManager agents.
  - `GM Reviewers N` for agents currently recorded or running as GoalManager reviewers.
- Idle-but-assigned session contacts are included in the aggregate counts so the UI reflects session assignment, while process slot numbers remain internal routing details.

## Files touched

- `src/services/codex/service.json`
- `src/services/claude/service.json`
- `src/services/gemini/service.json`
- `src/runtime/session_view.py`
- `src/runtime/http_handler.py`
- `src/runtime/cli_service_adapter.py`
- `src/runtime/html_renderer.py`
- `tests/test_bootstrap_service_manager.py`
- `tests/test_goal_manager_compact.py`
- `tests/test_entrance_page.py`

## Verification

- `python3 -m py_compile src/runtime/session_view.py src/runtime/http_handler.py src/runtime/html_renderer.py src/runtime/cli_service_adapter.py`
- `PYTHONPATH=./src python3 -m unittest tests.test_bootstrap_service_manager.BootstrapManifestTests.test_provider_descriptors_default_to_ten_workers -v`
- `python3 -m unittest tests.test_bootstrap_service_manager.BootstrapManifestTests.test_provider_descriptors_default_to_ten_workers tests.test_goal_manager_compact.GoalManagerCompactTests.test_session_agent_assignment_counts_split_goal_manager_and_assigned_agents tests.test_goal_manager_compact.GoalManagerCompactTests.test_build_worker_count_summary_counts_running_replying_and_reviewing tests.test_goal_manager_compact.GoalManagerCompactTests.test_build_worker_count_summary_uses_bound_service_when_worker_missing tests.test_goal_manager_compact.GoalManagerCompactTests.test_build_worker_count_summary_counts_queued_goal_manager_work_as_busy tests.test_goal_manager_compact.GoalManagerCompactTests.test_build_worker_count_summary_keeps_assigned_slots_when_nothing_is_executing tests.test_session_listing.SessionListingTests.test_session_agent_assignment_counts_track_reviewers_separately tests.test_entrance_page.EntrancePageTests.test_main_page_renders_latest_first_workspace_header_and_fixed_composer`
- `PYTHONPATH=./src python3 -m unittest tests.test_goal_manager_compact.GoalManagerCompactTests.test_session_agent_assignment_counts_split_goal_manager_and_assigned_agents tests.test_session_listing.SessionListingTests.test_session_agent_assignment_counts_track_reviewers_separately tests.test_entrance_page.EntrancePageTests.test_main_page_renders_latest_first_workspace_header_and_fixed_composer -v`
- `PYTHONPATH=./src python3 -m unittest tests.test_session_listing.SessionListingTests.test_session_agent_assignment_counts_include_idle_assignments -v`
- `PYTHONPATH=./src python3 -m unittest tests.test_bootstrap_service_manager.BootstrapManifestTests.test_provider_descriptors_default_to_ten_workers tests.test_session_listing.SessionListingTests.test_session_agent_assignment_counts_track_reviewers_separately tests.test_session_listing.SessionListingTests.test_session_agent_assignment_counts_include_idle_assignments tests.test_entrance_page.EntrancePageTests.test_main_page_renders_latest_first_workspace_header_and_fixed_composer -v`
- `PYTHONPATH=./src python3 -m unittest tests.test_goal_manager_compact.GoalManagerCompactTests.test_session_agent_assignment_counts_split_goal_manager_and_assigned_agents tests.test_goal_manager_compact.GoalManagerCompactTests.test_build_worker_count_summary_counts_running_replying_and_reviewing tests.test_goal_manager_compact.GoalManagerCompactTests.test_build_worker_count_summary_uses_bound_service_when_worker_missing tests.test_goal_manager_compact.GoalManagerCompactTests.test_build_worker_count_summary_counts_queued_goal_manager_work_as_busy tests.test_goal_manager_compact.GoalManagerCompactTests.test_build_worker_count_summary_keeps_assigned_slots_when_nothing_is_executing -v`
- `PYTHONPATH=./src python3 -m unittest tests.test_bootstrap_service_manager tests.test_session_listing tests.test_entrance_page tests.test_goal_manager_compact -q` currently fails in existing Codex provider tests because their `FakeProc` lacks the new `stdin` attribute used by `src/runtime/providers/codex.py`; this is outside the session-count/capacity path.
- Browser verification with headless Chrome against a rendered session-card fixture:
  - `Agents 3` rendered.
  - `GM Reviewers 2` rendered.
  - legacy `.goal-marker` slot marker markup was absent.
  - provider/process slot text was absent from the card text.
  - screenshot artifact: `.temp/session-box-counts-browser/session-box-counts.png`.
- Live bridge probe: `https://127.0.0.1:4123/health` returned connection refused, so live authenticated verification was not available in this pass.

## Remaining risk

The focused browser verification used a rendered fixture for the session-card display instead of a live authenticated HTTPBridge session because the local bridge was not listening on the default health URL during this pass. The summary data plumbing and renderer behavior are covered by focused unit tests, but live runtime verification still depends on authenticated bridge access and current session state.
