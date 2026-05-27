# Entrance Routing Regression Fix

- Restored Entrance-first behavior by keeping the shipped `canonical-development-routing` launcher skill non-default (`route_when_unhandled=false`), so launcher-only Entrance sessions no longer auto-delegate ordinary prompts into the canonical development parent.
- Kept `communication_agent_enabled=true` unchanged. Explicit routing and existing session-level `route_when_unhandled=true` compatibility paths remain covered by focused tests.

Files touched:
- `plugins/aize-entrance/units/entrance/unit.json`
- `tests/test_entrance_page.py`
- `tests/test_http_handler_goal_save.py`
- `tests/test_session_template.py`

Verification:
- `PYTHONPATH=./src python3 -m unittest tests.test_entrance_page.EntrancePageTests.test_matching_communication_skill_routes_launcher_template_keeps_unhandled_prompts_in_entrance tests.test_entrance_page.EntrancePageTests.test_materialize_launcher_route_does_not_auto_delegate_by_default tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_message_prompt_with_launcher_only_entrance_does_not_auto_delegate tests.test_session_template.SessionTemplateLauncherTests.test_entrance_unit_provisions_code_based_interactive_skill`
- `PYTHONPATH=./src python3 -m unittest tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_message_prompt_keeps_entrance_request_inside_entrance_before_goal_manager_routing tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_message_prompt_with_entrance_target_stays_in_entrance_before_delegation tests.test_entrance_page.EntrancePageTests.test_materialize_direct_development_route_launches_canonical_parent`

Residual risk:
- Persisted Entrance sessions that already saved an explicit session-level `route_when_unhandled=true` routing skill will keep using that stored rule until the session metadata is updated or the session is recreated.
