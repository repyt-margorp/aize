# Entrance Routing Default Fix

## Behavior changed

- Entrance launcher template routing no longer treats every unhandled input as AIze Development work.
- The canonical development routing skill remains present but `route_when_unhandled` stays disabled, so ordinary Entrance conversation remains in Entrance.
- Explicit session-level default routes still work when a session deliberately has a routing skill with `route_when_unhandled=true`.

## Files touched

- `plugins/aize-entrance/units/entrance/unit.json`
- `tests/test_entrance_page.py`
- `tests/test_session_template.py`

## Verification

- `python3 -m unittest tests.test_entrance_page.EntrancePageTests.test_matching_communication_skill_routes_prefers_explicit_default_route tests.test_entrance_page.EntrancePageTests.test_matching_communication_skill_routes_launcher_template_does_not_auto_route_by_default tests.test_entrance_page.EntrancePageTests.test_materialize_launcher_route_does_not_auto_delegate_by_default tests.test_session_template.SessionTemplateLauncherTests.test_entrance_unit_provisions_code_based_interactive_skill -v`

## Residual risk

- Requests that truly need implementation must be handled by Entrance/WorkerAgent progress management or by an explicit routing skill, not by blanket launcher fallback.
