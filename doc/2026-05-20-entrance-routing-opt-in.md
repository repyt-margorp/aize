## Summary

- Disabled the shipped Entrance unit's implicit development auto-routing so prompts are no longer delegated to the development parent by default.
- Tightened communication route matching so `routing_tags` only apply when a routing skill explicitly sets `allow_tag_routing=true`.

## Files Touched

- `src/runtime/http_handler.py`
- `plugins/aize-entrance/units/entrance/unit.json`
- `tests/test_entrance_page.py`
- `tests/test_session_template.py`

## Verification

- `python3 -m unittest tests.test_entrance_page tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_message_prompt_routes_entrance_request_through_development_child_proxy_path`
- `python3 -m unittest tests.test_session_template.SessionTemplateLauncherTests.test_entrance_unit_provisions_code_based_interactive_skill`

## Remaining Risk

- Existing sessions that already persisted `route_when_unhandled=true` or `allow_tag_routing=true` will keep that behavior until their stored session skills are updated or the session is recreated from the revised unit definition.
