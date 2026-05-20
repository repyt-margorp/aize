Implemented the latest Entrance/HttpBridge request to expand the default provider pools and replace workspace/session slot-number badges with explicit agent counts.

Behavior changed:
- `src/services/codex/service.json`, `src/services/claude/service.json`, and `src/services/gemini/service.json` now default their provider pools to `10`.
- Session and Workspace cards now show assigned non-GoalManager agent counts and GoalManager reviewer counts instead of fixed slot numbers.
- The count display is driven from persisted session agent assignments and explicit GoalManager role state, not text heuristics.

Files touched:
- `src/services/codex/service.json`
- `src/services/claude/service.json`
- `src/services/gemini/service.json`
- `doc/2026-03-23-current-architecture-status.md`
- `src/runtime/session_view.py`
- `src/runtime/http_handler.py`
- `src/runtime/html_renderer.py`
- `tests/test_bootstrap_service_manager.py`
- `tests/test_session_listing.py`
- `tests/test_entrance_page.py`

Verification:
- `python3 -m unittest tests.test_bootstrap_service_manager tests.test_session_listing tests.test_entrance_page tests.test_goal_manager_compact -q`
- `node .temp/session_box_counts_static_verify.js`
- Browser verifier artifacts: `.temp/session-box-counts-browser/index.html`, `.temp/session-box-counts-browser/dom.html`, `.temp/session-box-counts-browser/session-box-counts.png`

Remaining risk:
- The UI count badges reflect explicit assignment state and running GoalManager reviewer state. If a future workflow introduces additional durable reviewer roles, those roles should be added explicitly rather than inferred.
