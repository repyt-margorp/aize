## Entrance-first routing restore

- Restored the `entrance.service` launcher template so `canonical-development-routing` does not use `route_when_unhandled` by default. Ordinary unhandled dialogue now stays in Entrance unless explicit routing or follow-up logic forwards it.
- Updated focused regression tests to match the intended template behavior while preserving explicit delegated-routing coverage for AIze Development child-session materialization.
- Files touched: `plugins/aize-entrance/units/entrance/unit.json`, `tests/test_session_template.py`, `tests/test_entrance_page.py`.
- Verification run: `python3 -m unittest tests.test_session_template.SessionTemplateLauncherTests.test_entrance_unit_provisions_code_based_interactive_skill tests.test_session_template.SessionTemplateLauncherTests.test_entrance_unit_launch_supports_multiple_instances tests.test_entrance_page.EntrancePageTests.test_matching_communication_skill_routes_launcher_template_keeps_unhandled_prompts_in_entrance tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_message_prompt_keeps_entrance_request_inside_entrance_before_goal_manager_routing`.
- Residual risk: broader `pytest` coverage could not be used here because `pytest` is not installed in this runtime.
