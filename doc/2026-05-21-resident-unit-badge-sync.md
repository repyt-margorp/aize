# Resident Unit Badge Sync

Behavior changed:
- Session summaries now mark a session as `Resident Unit` when it is the active `last_session_id` recorded for a registered Unit, even if the stored session group is still `user`.
- Reused canonical Unit parent sessions now refresh the registered Unit state so the Unit points at the live reused session instead of a stale older session.

Files touched:
- `src/runtime/session_view.py`
- `src/runtime/http_handler.py`
- `tests/test_session_listing.py`
- `tests/test_http_handler_goal_save.py`
- `tests/test_entrance_page.py`

Verification run:
- `python3 -m unittest tests.test_session_listing.SessionListingTests.test_session_registration_metadata_marks_registered_unit_session_resident tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_get_sessions_prefilters_to_recent_and_resident_sessions tests.test_entrance_page.EntrancePageTests.test_materialize_communication_routed_child_session_prefers_registered_bug_hunting_parent_over_existing_child`
- Browser check on an isolated rendered HttpBridge page served from `./.temp/resident-unit-verification.html` with headless Chrome. Confirmed two visible `Resident Unit` badges for `Entrance` and `AIze Development`.

Remaining risk:
- The browser verification used an isolated renderer page with seeded session summaries rather than a restarted live runtime, so live-runtime confirmation still depends on the next runtime refresh/restart picking up this code.
