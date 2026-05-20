# Entrance Routing Default Fix

## Behavior changed

- Entrance launcher sessions no longer use `route_when_unhandled=true` as a blanket default route into `AIze Development`.
- Messages sent to Entrance now stay in the Entrance conversation first. The lightweight handler only answers empty/status/help/ping-style messages; other text is declined so the Entrance communication agents can decide whether to answer, clarify, or delegate.
- Explicit session-level routing skills that already persist `route_when_unhandled=true` still work for compatibility. The fix is scoped to the shipped Entrance launcher template so new/manual Entrance instances do not bypass their own GoalManager/agents.

## Root cause

- The active Entrance history showed ordinary messages and UI/design feedback getting an immediate `Routed to AIze Development...` acknowledgement.
- The shipped Entrance unit had `canonical-development-routing.route_when_unhandled=true`.
- `_matching_communication_skill_routes()` falls back from empty `session_skills` to launcher-template skills, so a new Entrance instance with no persisted skills still matched the canonical development route before Entrance could reason about the message.

## Files touched

- `plugins/aize-entrance/units/entrance/unit.json`
- `tests/test_entrance_page.py`
- `tests/test_session_template.py`

## Verification

- `python3 -m unittest tests.test_entrance_page.EntrancePageTests.test_matching_communication_skill_routes_prefers_explicit_default_route tests.test_entrance_page.EntrancePageTests.test_matching_communication_skill_routes_launcher_template_does_not_auto_route_by_default tests.test_entrance_page.EntrancePageTests.test_materialize_launcher_route_does_not_auto_delegate_by_default tests.test_session_template.SessionTemplateLauncherTests.test_entrance_unit_provisions_code_based_interactive_skill -v`

## Residual risk

- Existing persisted Entrance sessions that already saved a default routing skill can still preserve their stored behavior. The live session used in the reported history had an empty persisted skill list, so it follows the launcher-template fix after restart/reload.
