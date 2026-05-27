# Agent Capacity And Session Counts

## Behavior Changed
- Default provider process pools are 10 services for Codex, Claude, and Gemini through the service descriptors.
- Session cards surface aggregate assignment counts: non-GoalManager `Agents` and `GM Reviewers`.
- Process indexes and provider session slots remain runtime routing details, not session-card display labels.
- The HTTPBridge adapter fallback count shim now matches `runtime.session_view.session_agent_assignment_counts`, including canonical service de-duplication for provider-session agent IDs.

## Files Touched
- `src/services/codex/service.json`
- `src/services/claude/service.json`
- `src/services/gemini/service.json`
- `src/runtime/html_renderer.py`
- `src/runtime/session_view.py`
- `src/runtime/cli_service_adapter.py`
- `tests/test_bootstrap_service_manager.py`
- `tests/test_entrance_page.py`
- `tests/test_session_listing.py`
- `tests/test_goal_manager_compact.py`

## Verification
- `python3 -m unittest tests.test_session_listing tests.test_bootstrap_service_manager tests.test_entrance_page`
  - Result: `OK` (`72` tests).
- `python3 -m py_compile src/runtime/session_view.py src/runtime/http_handler.py src/runtime/html_renderer.py src/runtime/cli_service_adapter.py`
  - Result: no syntax errors.
- Runtime state check against `./.aize-runtime/state/services.json`
  - Result: canonical pools include `10` Claude and `10` Gemini services; canonical Codex services are present through `service-codex-010` alongside extra task-specific Codex services.
- Live health probe against `https://127.0.0.1:64123/health` with certificate verification disabled.
  - Result: HTTP `200`, `service-http-001`, process `proc-service-http-001-f6bc244a`.
- Browser verification with headless Chrome:
  - `google-chrome --headless=new --disable-gpu --no-sandbox --disable-extensions --dump-dom file://$PWD/.temp/session-box-card-minimal.html`
  - Result: dumped visible text contains `Agents 3` and `GM Reviewers 2`; no `Agent 1`-style slot label was present.
- Browser-backed UI probe attempted against the live bridge with a minted `root` session token:
  - The existing probe completed through its deterministic HTTP fallback (`verification_mode=http_api_full`) with root and child page UI markers present and goal-save flow OK.
  - Direct live root/session page fetches remained too slow for a full DOM assertion in this pass, so the session-card label assertion used the focused headless Chrome fixture above.

## Residual Risk
- Live authenticated full-page SessionMap rendering remains latency-sensitive in the current runtime state; `/health` is healthy, but full root/session page fetches exceeded the local 30s probe window during this pass.
- The focused browser fixture verifies the displayed card labels and absence of slot identities; the data plumbing is covered by the unit tests above.

## Prior Verification
- `PYTHONPATH=./src python3 -m unittest tests.test_bootstrap_service_manager.BootstrapManifestTests.test_provider_descriptors_default_to_ten_workers tests.test_entrance_page.EntrancePageTests.test_main_page_renders_latest_first_workspace_header_and_fixed_composer tests.test_session_listing.SessionListingTests.test_session_agent_assignment_counts_track_reviewers_separately tests.test_session_listing.SessionListingTests.test_session_agent_assignment_counts_include_idle_assignments tests.test_goal_manager_compact.GoalManagerCompactTests.test_session_agent_assignment_counts_split_goal_manager_and_assigned_agents tests.test_goal_manager_compact.GoalManagerCompactTests.test_build_worker_count_summary_counts_running_replying_and_reviewing tests.test_goal_manager_compact.GoalManagerCompactTests.test_build_worker_count_summary_uses_bound_service_when_worker_missing tests.test_goal_manager_compact.GoalManagerCompactTests.test_build_worker_count_summary_counts_queued_goal_manager_work_as_busy tests.test_goal_manager_compact.GoalManagerCompactTests.test_build_worker_count_summary_keeps_assigned_slots_when_nothing_is_executing -v`
  - Result: `OK`
- `PYTHONPATH=./src python3 -m unittest tests.test_session_listing tests.test_entrance_page -q`
  - Result: `OK`
- `python3 .temp/verify_agent_counts_browser.py`
  - Generated `.temp/agent_counts_browser.html` from `runtime.html_renderer.render_main_page` with a SessionMap summary containing `assigned_agent_count=2` and `goal_manager_reviewer_count=1`.
- `google-chrome --headless=new --disable-gpu --no-sandbox --window-size=1365,900 --screenshot=.temp/agent_counts_browser-rerun.png --dump-dom file://$PWD/.temp/agent_counts_browser.html`
  - Result: Chrome exit `0`; dumped DOM contains `Agents 2` and `GM Reviewers 1`; no `Agent N` or `Slot N` badge text was emitted.
- Parsed the headless Chrome DOM visible text and confirmed `Agents 2`, `GM Reviewers 1`, and no `Agent N`/`Slot N` visible-text matches; screenshot size was 182047 bytes.

## Prior Residual Risk
- Existing JSON fields such as `assigned_slots` remain in provider-level runtime summaries for compatibility, but the user-facing SessionMap no longer renders them as slot identities.
