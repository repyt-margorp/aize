## Summary

Prevented empty or status-only communication-session goal turns from re-queueing GoalManager after the session had already settled into an `all_clear` idle state.

## Files Touched

- `src/runtime/agent_service.py`
- `tests/test_goal_manager_compact.py`

## Behavior Change

- Post-turn GoalManager follow-up now skips communication-session goal/status turns when the turn only consumed goal-management inputs and produced neither user-visible text nor delegated child work.
- Dedicated GoalManager review turns still remain excluded, and ordinary worker turns still enqueue GoalManager review when they produce visible progress or child-spawn work.
- This keeps resident Entrance-style communication sessions idle after an all-clear reconcile instead of immediately flipping back to `queued`/`running` on a no-op status turn.

## Verification

- `python3 -m unittest tests.test_goal_manager_compact.GoalManagerCompactTests.test_goal_manager_post_turn_does_not_enqueue_followup_for_its_own_turn tests.test_goal_manager_compact.GoalManagerCompactTests.test_goal_manager_post_turn_skips_empty_communication_goal_status_turns tests.test_entrance_page.EntrancePageTests.test_communication_goal_cycle_completes_only_on_visible_reply tests.test_entrance_page.EntrancePageTests.test_goal_manager_review_preserves_prompt_cycle_completion -q`
- `python3 -m unittest tests.test_session_listing.SessionListingTests tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_get_sessions_prefilters_to_recent_and_resident_sessions tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_root_page_idle_reconcile_skips_sessions_with_goal_manager_already_queued tests.test_entrance_page.EntrancePageTests.test_entrance_status_events_refresh_immediately_and_state_poll_converges tests.test_entrance_page.EntrancePageTests.test_communication_history_prefers_single_interactive_reply_over_duplicate_agent_event -q`
- `python3 -m py_compile src/runtime/agent_service.py tests/test_goal_manager_compact.py tests/test_entrance_page.py`

## Remaining Risk

- The new guard is intentionally scoped to communication sessions with empty/status-only goal-management turns. If a future communication-session workflow needs silent background work to trigger GoalManager review without visible text or child delegation, it will need an explicit persisted signal instead of relying on this empty-turn path.
