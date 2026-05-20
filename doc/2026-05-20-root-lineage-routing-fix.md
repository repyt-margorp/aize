# 2026-05-20 Root Lineage Routing Fix

## Behavior changed

- Canonical development routing now reuses only the registered `aize-development.bug-hunting` parent session launched under `default`.
- Ad hoc sessions that merely carry the `aize.development` canonical key are no longer accepted as reusable AIze Development parents.
- When the canonical parent is reused or created, its goal text is synchronized from the canonical Unit definition so the parent goal stays aligned with the actual development workflow contract.

## Files touched

- `src/runtime/http_handler.py`
- `tests/test_entrance_page.py`
- `plugins/aize-entrance/units/entrance/unit.json`
- `plugins/aize-development/units/bug-hunting/unit.json`
- `doc/2026-05-20-root-goal-lineage.md`

## Verification run

- `python3 -m unittest tests.test_entrance_page.EntrancePageTests.test_materialize_communication_routed_child_session_ignores_noncanonical_development_parent tests.test_entrance_page.EntrancePageTests.test_materialize_communication_routed_child_session_prefers_top_level_canonical_parent_when_multiple_match tests.test_entrance_page.EntrancePageTests.test_materialize_communication_routed_child_session_prefers_registered_bug_hunting_parent_over_existing_child tests.test_entrance_page.EntrancePageTests.test_materialize_communication_routed_child_session_prefers_registered_parent_even_when_parent_goal_is_complete tests.test_session_template.SessionTemplateLauncherTests.test_bug_hunting_unit_provisions_canonical_session_skills tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_message_prompt_routes_entrance_request_through_development_child_proxy_path`
- Result: `OK` with 6 focused tests.
- Isolated runtime smoke check:
  - started with `PYTHONPATH=./src AIZE_RUNTIME_ROOT=./.temp/runtime-smoke-root AIZE_HTTP_PORT=44123 AIZE_HTTP_HOST=127.0.0.1 AIZE_TLS=false python3 -m cli.run_aize_unit --runtime-root ./.temp/runtime-smoke-root`
  - verified `http://127.0.0.1:<alternate-port>/health` returned `200` with `{"ok": true, "service_id": "service-http-001", ...}`
  - verified `http://127.0.0.1:<alternate-port>/` returned `200`
  - stopped the isolated runtime after verification

## Residual risk

- Other older tests or workflows that relied on reusing ad hoc compatibility parents with only a matching canonical session key will now need to create or attach to the canonical bug-hunting Unit parent instead.
