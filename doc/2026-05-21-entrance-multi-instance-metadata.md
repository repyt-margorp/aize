Entrance now supports multiple purpose-specific launches while preserving per-session unit metadata in SessionMap.

Behavior changed:
- `plugins/aize-entrance/units/entrance/unit.json` uses `instance_policy: "multi"` so launching Entrance does not imply a singleton session.
- Launched unit sessions persist `launcher_unit_kind`, `launcher_unit_class`, and `launcher_instance_policy` along with the existing unit/template/display metadata.
- SessionMap summary classification treats interface/service-backed unit launches such as Entrance as resident-unit sessions even when the unit registry can only point `last_session_id` at the most recent launch.
- Canonical unhandled implementation routing remains enabled on Entrance and stays scoped by `route_parent_scope: "origin_session"` so separate Entrance instances keep separate routed development parents.

Files touched:
- `plugins/aize-entrance/units/entrance/unit.json`
- `src/runtime/persistent_state_pkg/conversation.py`
- `src/runtime/session_view.py`
- `src/session_template.py`
- `tests/test_session_listing.py`
- `tests/test_session_template.py`
- `tests/test_entrance_page.py`

Verification:
- `python3 -m unittest tests.test_session_listing tests.test_session_template tests.test_entrance_page -q`
- Headless Chrome rendered `.temp/entrance-multi-sessionmap.html`, dumped `.temp/entrance-multi-sessionmap.dom`, and captured `.temp/entrance-multi-sessionmap.png`.
- Browser DOM checks confirmed both `Entrance A` and `Entrance B` render, each shows `Resident Unit` and `Unit · Entrance`, `entrance.service` metadata is present, and goal/runtime badges remain visible.

Remaining risk:
- SessionMap resident classification now relies on stored launcher unit kind/class for interface/service launches. If an older runtime contains pre-migration Entrance sessions without those launcher fields, those sessions will still depend on their persisted registry association until they are relaunched or backfilled.
