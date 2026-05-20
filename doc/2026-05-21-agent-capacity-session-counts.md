# Agent Capacity and Session Counts

## Behavior

- Default local provider pools now start 10 Codex, 10 Claude, and 10 Gemini agent processes unless overridden by the existing pool size environment variables.
- HTTPBridge session cards no longer render provider slot markers as user-facing session meaning.
- Session cards now show aggregate session participation counts:
  - `Agents N` for assigned non-GoalManager agents.
  - `GM Reviewers N` for agents currently recorded or running as GoalManager reviewers.

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
- `python3 -m unittest tests.test_bootstrap_service_manager.BootstrapManifestTests.test_provider_descriptors_default_to_ten_workers tests.test_goal_manager_compact.GoalManagerCompactTests.test_session_agent_assignment_counts_split_goal_manager_and_assigned_agents tests.test_goal_manager_compact.GoalManagerCompactTests.test_build_session_runtime_summary_prefers_active_worker tests.test_entrance_page.EntrancePageTests.test_main_page_renders_latest_first_workspace_header_and_fixed_composer -v`
- Browser verification with headless Chrome against a rendered session-card fixture:
  - `Agents 2` rendered.
  - `GM Reviewers 1` rendered.
  - legacy `.goal-marker` slot marker markup was absent.
  - screenshot artifact: `.temp/session-box-counts-browser/session-box-counts-static.png`.

## Remaining risk

The focused browser verification used a rendered fixture for the session-card display instead of a live authenticated HTTPBridge session. The summary data plumbing and renderer behavior are covered by focused unit tests, but live runtime verification still depends on authenticated bridge access and current session state.
