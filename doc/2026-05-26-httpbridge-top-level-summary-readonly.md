Behavior changed:
- `GET /sessions`, `GET /overview`, and the root SessionMap bootstrap now keep their per-session summary reads read-only instead of running stale-service reconciliation and service-snapshot rewrite work for every visible session.
- List-view audit status now uses a shallow fast path based on the active GoalManager state plus the preferred service audit record, which preserves top-level goal/runtime visibility without scanning every joined service file on each summary refresh.

Files touched:
- `src/runtime/http_handler.py`
- `src/runtime/session_view.py`
- `src/runtime/cli_service_adapter.py`
- `src/runtime/persistent_state_pkg/agent_audit.py`
- `tests/test_http_handler_goal_save.py`
- `tests/test_session_listing.py`

Verification:
- `PYTHONPATH=./src python3 -m unittest tests.test_session_listing.SessionListingTests.test_session_summary_skips_reconcile_for_read_only_views tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_get_sessions_and_overview_skip_reconcile_on_summary_reads tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_get_sessions_uses_ttl_cache_for_browser_cache_busters tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_get_overview_uses_ttl_cache_for_browser_cache_busters -v`
- Direct local handler timing against the current `./.aize-runtime` state with authenticated `repyt` context:
  - `GET /sessions?live=1` completed in about `0.324s` for `126` visible session summaries.
  - `GET /overview?live=1` completed in about `0.349s` for the same scope.
- Direct function timing against the same runtime state showed the summary-read path over 20 sessions dropping from about `4.285s` with reconciliation enabled to about `0.001s` with the new read-only path.

Remaining risk:
- The read-only top-level fast path no longer inspects every non-preferred service audit file on each refresh, so a panic recorded only on an older non-preferred child service may wait until a detail view or another mutating reconciliation pass to be normalized or surfaced. That tradeoff is intentional to keep SessionMap and overview requests responsive.
