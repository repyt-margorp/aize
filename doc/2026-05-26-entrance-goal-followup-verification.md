# Entrance Goal Follow-up Verification

## Behavior checked
- Re-verified the active Entrance/GoalManager change bundle covering goal-state visibility,
  runtime-backed request visibility, canonical development routing lineage, and related
  Session UI status surfaces.
- Confirmed the current targeted regression suite still passes together, with no new failures
  in the reviewed areas.
- Confirmed the latest post-restart live browser verification in the session timeline still shows
  sanitized Entrance status behavior on the live runtime.

## Files touched
- `doc/2026-05-26-entrance-goal-followup-verification.md`

## Verification
- `PYTHONPATH=./src python3 -m unittest tests.test_service_control tests.test_http_handler_goal_save tests.test_session_listing tests.test_entrance_page tests.test_goal_manager_compact -q`
- `PYTHONPATH=./src python3 -m unittest tests.test_service_control.ServiceControlParserTests.test_handoff_spawn_request_uses_canonical_development_parent_for_communication_session tests.test_service_control.ServiceControlParserTests.test_communication_spawn_handoff_uses_canonical_development_parent tests.test_service_control.ServiceControlParserTests.test_route_spawn_request_to_communication_child_session_creates_canonical_child -v`
- `PYTHONPATH=./src python3 -m unittest tests.test_goal_manager_compact.GoalManagerCompactTests.test_actionable_post_turn_input_present_ignores_restart_resume_for_communication_sessions tests.test_goal_manager_compact.GoalManagerCompactTests.test_session_turn_completed_input_skips_status_only_communication_turns tests.test_goal_manager_compact.GoalManagerCompactTests.test_maybe_resume_after_restart_skips_idle_continuous_communication_goal -v`
- Session timeline evidence from `service-codex-entrance-status-banner-001` at `2026-05-25T23:44:50Z`:
  live Chrome DevTools check against `https://127.0.0.1:64123/units/entrance?session_id=c5bf223f03be6f0c`
  reported `hasOldFullClaim=false`, `hasParallelClaim=false`, `hasRuntimeBackedWorker=true`,
  `hasGoalActive=true`, `hasGoalInProgress=true`, `hasExecuting=true`, and `hasAllClear=true`.

## Remaining risk
- The targeted routing and restart-resume guards still pass after the latest restart cycle.
  Remaining risk is limited to older persisted free-text worker claims that lack runtime worker
  state; those are intentionally hidden by the sanitizer and chat filter path.
