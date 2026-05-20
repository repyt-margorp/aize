# Entrance Origin-Scoped Development Routing

## Behavior changed

- Entrance's canonical development routing now declares `route_parent_scope: origin_session`.
- The shipped `entrance.service` launcher route keeps `route_when_unhandled: false`, so untagged `SendPrompt` text stays in Entrance first and only explicit skill routing delegates into canonical development.
- When session snapshots are shallow copies with `session_skills` removed, origin-scoped parent resolution now falls back to canonical parent session metadata and the registered `aize-development.bug-hunting` unit state.
- Repeated work from the same Entrance reuses its own canonical `AIze Development` parent, while different Entrance sessions still keep separate downstream parent trees.

## Files touched

- `plugins/aize-entrance/units/entrance/unit.json`
- `src/runtime/http_handler.py`
- `src/runtime/http_dispatch.py`
- `src/runtime/persistent_state_pkg/_core.py`
- `tests/test_entrance_page.py`
- `tests/test_http_handler_goal_save.py`
- `tests/test_http_dispatch.py`
- `tests/test_session_template.py`

## Verification

- `PYTHONPATH=./src python3 -m unittest tests.test_entrance_page.EntrancePageTests.test_matching_communication_skill_routes_launcher_template_does_not_auto_route_by_default tests.test_entrance_page.EntrancePageTests.test_materialize_launcher_route_does_not_auto_delegate_by_default tests.test_entrance_page.EntrancePageTests.test_materialize_direct_development_route_launches_canonical_parent tests.test_session_template.SessionTemplateLauncherTests.test_entrance_unit_provisions_code_based_interactive_skill tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_message_prompt_runs_entrance_handler_before_unhandled_development_route tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_message_prompt_with_entrance_target_stays_in_entrance_before_delegation -v`
- `PYTHONPATH=./src python3 -m unittest tests.test_http_dispatch tests.test_entrance_page tests.test_http_handler_goal_save tests.test_session_template -q`

## Follow-up

- Updated `tests/test_entrance_page.py` to assert the current goal-manager review enqueue path via `kind="goal_manager_review"` instead of an older JSON source literal. The runtime still enqueues goal-manager review work before building the communication dispatch plan; the test now matches the maintained implementation shape.
- Updated `tests/test_http_handler_goal_save.py` to match the maintained dispatch priority order: `goal_manager_review` is injected before `http_user_dialogue` and `interactive_worker_request`.
- Re-verified the two user-visible routing guarantees for this goal at handler level: lightweight Entrance prompts remain in Entrance with no delegated child session, and explicit development routing can still materialize the canonical `AIze Development` workflow when a route is selected deliberately.

## Residual risk

- Existing already-created canonical development parents keep their stored lineage. The new fallback reuses only parents whose `origin_session_id` matches the current Entrance session, so any historically mis-scoped parent remains isolated until recreated.
