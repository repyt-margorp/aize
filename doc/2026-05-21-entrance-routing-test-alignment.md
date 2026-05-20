# Entrance Routing Test Alignment

- Updated stale routing coverage so launcher-only Entrance sessions no longer imply automatic delegation into `AIze Development`.
- The adjusted regression test now exercises the supported compatibility path: an explicit session-level `canonical-development-routing` rule can still reuse the origin-scoped registered development parent even when the visible session list is shallow.

Files touched:
- `tests/test_entrance_page.py`

Verification:
- `python3 -m unittest tests.test_entrance_page.EntrancePageTests.test_matching_communication_skill_routes_launcher_template_does_not_auto_route_by_default tests.test_entrance_page.EntrancePageTests.test_materialize_launcher_route_does_not_auto_delegate_by_default tests.test_entrance_page.EntrancePageTests.test_materialize_explicit_route_reuses_origin_scoped_registered_parent_from_shallow_sessions`
- `python3 -m unittest tests.test_session_template.SessionTemplateLauncherTests.test_entrance_unit_launch_supports_multiple_instances`
- `python3 -m unittest tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_message_prompt_runs_entrance_handler_before_unhandled_development_route`
- `python3 -m unittest tests.test_http_handler_goal_save`

Residual risk:
- This change updates regression coverage only. Existing persisted Entrance sessions that still carry explicit `route_when_unhandled=true` session skills will keep that compatibility behavior until those stored session skills are changed or the session is recreated.
