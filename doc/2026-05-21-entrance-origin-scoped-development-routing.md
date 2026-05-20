# Entrance Origin-Scoped Development Routing

## Behavior changed

- Entrance's canonical development routing now declares `route_parent_scope: origin_session`.
- Unhandled work routed from one Entrance session now reuses that Entrance's own canonical `AIze Development` parent instead of attaching to a parent created by a different Entrance session.
- Repeated work from the same Entrance still reuses its existing canonical development parent, so topic-specific Entrance sessions can keep separate downstream context.

## Files touched

- `plugins/aize-entrance/units/entrance/unit.json`
- `src/runtime/http_handler.py`
- `src/runtime/persistent_state_pkg/_core.py`
- `tests/test_entrance_page.py`
- `tests/test_session_template.py`

## Verification

- `python3 -m unittest tests.test_entrance_page.EntrancePageTests.test_materialize_communication_routed_child_session_reuses_registered_parent_for_parallel_tasks tests.test_entrance_page.EntrancePageTests.test_materialize_communication_routed_child_session_keeps_origin_scoped_parent_per_entrance tests.test_session_template.SessionTemplateLauncherTests.test_entrance_unit_provisions_code_based_interactive_skill -v`

## Residual risk

- Existing already-created canonical development parents keep their stored lineage. The new scope prevents future cross-Entrance reuse but does not migrate historical sessions.
