## Summary

- Reduced HTTPBridge top-page load work for selected-session views.
- `GET /?session_id=...` no longer precomputes all visible session summaries on the initial HTML render when SessionMap is closed.
- Session navigation and board data now hydrate from the existing client refresh path after load instead of blocking the first response.

## Files Touched

- `src/runtime/http_handler.py`
- `tests/test_http_handler_goal_save.py`

## Verification

- `python3 -m unittest tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_root_page_renders_session_map_with_registered_unit_metadata tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_root_page_with_selected_session_skips_all_session_audit_prefetch`
- Isolated render-path benchmark against `.temp/debian-top-page-latency/.aize-runtime`:
  - Before fix: selected-session root render called `load_session_audit_summary` 110 times across 109 sessions and took about `0.323s`.
  - After fix: the same selected-session render called `load_session_audit_summary` once and took about `0.116s`.

## Remaining Risk

- Selected-session pages now ship an empty initial session index until the existing client-side `/sessions` refresh completes, so nav/session-board content may appear a moment later than the rest of the page on slow clients.
