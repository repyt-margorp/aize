# Entrance Routing Heuristic Removal

## Behavior changed

- Entrance communication prompts now run interactive Session Skill handlers before any development-route materialization.
- The canonical development route is no longer selected from prompt keywords by default. The Entrance unit disables tag routing for `canonical-development-routing`, so development routing must come from explicit skill configuration rather than text matching.
- `route_when_unhandled` is honored only after interactive handlers decline a prompt, instead of behaving like a pre-handler unconditional forwarding path.

## Files touched

- `src/runtime/http_handler.py`
- `tests/test_http_handler_goal_save.py`
- `plugins/aize-entrance/units/entrance/unit.json`

## Verification

- `python3 -m py_compile src/runtime/http_handler.py tests/test_http_handler_goal_save.py`
- `python3 -m unittest tests.test_entrance_page -q`
- `python3 -m unittest tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_message_prompt_runs_entrance_handler_before_unhandled_development_route tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_message_prompt_routes_entrance_request_through_development_child_proxy_path -q`

## Remaining risk

- Broader HTTPBridge routing behavior still needs full-suite coverage before cutover. The focused regression covers handled Entrance prompts not being forwarded to development just because a development routing skill exists.
