# Workspace, Entrance, and Skill Fixes

Date/time: 2026-05-20

## Behavior Changed

- Entrance realtime updates now reconnect after an EventSource/SSE error instead of staying disconnected until a full page refresh.
- WorkspaceView goal text is rendered as a readonly textarea so users can focus, select, and copy the goal text directly.
- WorkspaceView runtime journal support exposes metadata up front and loads the full runtime event log lazily when opened.
- Development prompts now require concise implementation logs under `./doc/` after code changes.
- Session skill guidance now states that Session skills are durable conventions, while AdaptiveSkill content should hold reusable repeated task code or procedures.

## Files Changed

- `AGENTS.md`
- `plugins/aize-development/units/bug-hunting/unit.json`
- `plugins/aize-development/units/minix-refactor/unit.json`
- `plugins/aize-entrance/units/entrance/unit.json`
- `src/runtime/html_renderer.py`
- `src/runtime/http_handler.py`
- `src/runtime/persistent_state_pkg/__init__.py`
- `tests/test_entrance_page.py`

## Verification

- `python3 -m py_compile src/runtime/http_handler.py src/runtime/html_renderer.py src/runtime/persistent_state_pkg/__init__.py`
- `PYTHONPATH=./src python3 -m unittest tests.test_entrance_page tests.test_goal_manager_compact`

## Residual Risk

- Entrance still has a polling fallback, but realtime reconnection should be watched in the browser after restart because SSE failures depend on active runtime/network behavior.
