# Unit Registry Launched Sessions

- The Units pane now tracks recent sessions launched by each unit instead of only the last session id.
- Interface-backed units such as Entrance expose recent launched session titles plus direct `Open Unit Interface` actions that reopen the matching unit UI with the correct `session_id`, while still keeping `Open Workspace` available.
- The primary `Open Last Session` control now prefers the unit interface when the selected unit has one, so Entrance reopens in its own interface instead of dropping straight into WorkspaceView.

Files touched:
- `src/runtime/http_handler.py`
- `src/runtime/html_renderer.py`
- `tests/test_entrance_page.py`
- `tests/test_unit_catalog.py`

Verification:
- `python3 -m unittest tests.test_unit_catalog tests.test_entrance_page.EntrancePageTests.test_main_page_unit_registry_renders_launched_session_controls tests.test_entrance_page.EntrancePageTests.test_entrance_page_renders_chat_polling_surface -v`
- Headless Chrome check against a mocked Units response confirmed the rendered Units DOM contains `Launched Sessions`, `Entrance Beta`, `Open Unit Interface`, `Open Workspace`, and `Open Latest Unit Session`.

Remaining risk:
- Browser verification used a mocked local Units payload instead of the live runtime, so the shipped behavior still depends on real `/units` data arriving in the same shape during full runtime use.
