HttpBridge now treats the browser `_=` cache-buster as transport noise instead of a forced live recompute for top-level session data. `GET /sessions` and `GET /overview` both reuse the shared TTL overview cache by default, and callers that truly need a fresh recompute must opt in with `live=1` (or `refresh=1`).

Files touched: `src/runtime/http_handler.py`, `tests/test_http_handler_goal_save.py`.

Verification run: `python3 -m unittest tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_get_overview_uses_ttl_cache_for_browser_cache_busters tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_get_sessions_uses_ttl_cache_for_browser_cache_busters tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_get_sessions_prefilters_to_recent_and_resident_sessions tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_cross_user_session_view_lists_session_owner_requests_for_owned_scope`

Remaining risk: the first uncached all-session overview is still expensive on very large runtimes. This change removes repeated recomputes from normal top-level polling, but it does not yet add a deeper persistent summary index for cold-cache scans.
