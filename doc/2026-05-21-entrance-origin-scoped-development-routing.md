# Entrance Origin-Scoped Development Routing

## Behavior changed

- Entrance's canonical development routing now declares `route_parent_scope: origin_session`.
- The shipped `entrance.service` launcher route again uses `route_when_unhandled: true`, so unhandled work-bearing prompts are delegated through the canonical development route without prompt-tag heuristics.
- When session snapshots are shallow copies with `session_skills` removed, origin-scoped parent resolution now falls back to canonical parent session metadata and the registered `aize-development.bug-hunting` unit state.
- Repeated work from the same Entrance reuses its own canonical `AIze Development` parent, while different Entrance sessions still keep separate downstream parent trees.

## Files touched

- `plugins/aize-entrance/units/entrance/unit.json`
- `src/runtime/http_handler.py`
- `src/runtime/persistent_state_pkg/_core.py`
- `tests/test_entrance_page.py`
- `tests/test_session_template.py`

## Verification

- `python3 -m unittest tests.test_entrance_page.EntrancePageTests.test_matching_communication_skill_routes_launcher_template_auto_routes_unhandled_work tests.test_entrance_page.EntrancePageTests.test_materialize_launcher_route_creates_canonical_development_child tests.test_entrance_page.EntrancePageTests.test_materialize_launcher_route_reuses_origin_scoped_registered_parent_from_shallow_sessions tests.test_entrance_page.EntrancePageTests.test_materialize_communication_routed_child_session_keeps_origin_scoped_parent_per_entrance tests.test_session_template.SessionTemplateLauncherTests.test_entrance_unit_provisions_code_based_interactive_skill -v`
- `python3 -m unittest tests.test_entrance_page tests.test_http_handler_goal_save tests.test_session_template -q`

## Residual risk

- Existing already-created canonical development parents keep their stored lineage. The new fallback reuses only parents whose `origin_session_id` matches the current Entrance session, so any historically mis-scoped parent remains isolated until recreated.
