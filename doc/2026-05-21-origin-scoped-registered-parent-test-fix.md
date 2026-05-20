# Origin-Scoped Registered Parent Test Fix

- No user-visible behavior changed.
- Updated the origin-scoped registered-parent regression so it reflects the current shipped Entrance routing policy, which no longer relies on launcher-level `route_when_unhandled=true`.
- The regression now verifies the same fallback behavior through an explicit session-level routing skill while shallow copied session summaries omit persisted `session_skills`.

Files touched:
- `tests/test_entrance_page.py`

Verification:
- `python3 -m unittest tests.test_entrance_page.EntrancePageTests.test_materialize_launcher_route_reuses_origin_scoped_registered_parent_from_shallow_sessions tests.test_entrance_page.EntrancePageTests.test_entrance_page_renders_chat_polling_surface -v`
- `python3 -m unittest tests.test_unit_catalog tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_get_units_includes_unit_launched_sessions -v`

Remaining risk:
- This change only corrects regression coverage. It does not broaden shipped routing behavior.
