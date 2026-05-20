Implemented the Entrance Unit metadata fix for multi-instance launches.

Behavior changed:
- `plugins/aize-entrance/units/entrance/unit.json` now declares `instance_policy` as `multi`.
- Unit launches persist launcher unit kind, unit class, and instance policy on the created session.
- Session summaries treat unit-backed communication sessions as ResidentUnit-style sessions, which also repairs older Entrance sessions that already have `launcher_unit_id=entrance.service` but lack the newly persisted launcher kind/class fields.
- SessionMap filtering uses the same resident-style metadata rule as summaries, so multiple Entrance sessions remain visible even when only one is the latest registered unit session.

Files touched:
- `plugins/aize-entrance/units/entrance/unit.json`
- `src/runtime/persistent_state_pkg/conversation.py`
- `src/session_template.py`
- `src/runtime/session_view.py`
- `src/runtime/http_handler.py`
- `tests/test_session_listing.py`
- `tests/test_session_template.py`
- `tests/test_entrance_page.py`

Verification:
- `python3 -m unittest tests.test_session_listing tests.test_session_template tests.test_entrance_page tests.test_http_handler_goal_save -q`
- Direct runtime inspection of session `8149fee8e6aeac43` returned `resident_unit_session=True`, `associated_unit_id=entrance.service`, and display name `Entrance`.
- Headless Chrome rendered `.temp/entrance-multi-sessionmap.html`, dumped `.temp/entrance-multi-sessionmap.dom`, and captured `.temp/entrance-multi-sessionmap.png`.
- Browser DOM checks confirmed `Entrance A`, `Resident Unit`, `Unit · Entrance`, `Goal Active`, `Goal In Progress`, `Runtime Idle`, and `All Clear`.

Remaining risk:
- Existing non-Entrance communication units with launcher metadata will now also render as ResidentUnit-style sessions. This matches the current interface-unit model, but a future non-resident communication unit should add explicit metadata if it needs different SessionMap treatment.
