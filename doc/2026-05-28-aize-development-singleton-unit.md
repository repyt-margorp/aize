# AIze Development Singleton Unit

## Behavior Changed
- AIze Development is visible in the public Unit catalog again, alongside Entrance.
- Entrance remains a multi-instance communication Unit.
- AIze Development is now a singleton service Unit. Launching it repeatedly reuses the existing canonical session instead of creating duplicate coordinator sessions.

## Files Touched
- `plugins/aize-development/plugin.json`
- `plugins/aize-development/units/bug-hunting/unit.json`
- `src/session_template.py`
- `tests/test_session_template.py`

## Verification
- Added coverage for public catalog visibility and singleton reuse.
- `PYTHONPATH=./src python3 -m unittest tests.test_session_template`
- `PYTHONPATH=./src python3 -m unittest tests.test_plugin_catalog`
- `PYTHONPATH=./src python3 -m unittest tests.test_entrance_page.EntrancePageTests.test_main_page_unit_registry_renders_launched_session_controls tests.test_entrance_page.EntrancePageTests.test_session_map_filter_keeps_multi_entrance_unit_sessions_visible`

## Remaining Risk
- Existing runtime state with older ad hoc AIze Development sessions may still need one launch of the singleton Unit to refresh the registered Unit state and launcher profile.
