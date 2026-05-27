## Behavior changed

- `/units` now returns the primary unit catalog payload without the legacy `apps` alias.
- `/session-templates` continues to expose the legacy `apps` alias for compatibility callers.

## Files touched

- `src/runtime/http_handler.py`
- `tests/test_http_handler_goal_save.py`

## Verification run

- `PYTHONPATH=./src python3 -m unittest tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_get_units_includes_unit_launched_sessions tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_get_session_templates_preserves_legacy_apps_alias`

## Residual risk

- Older external callers that still read `apps` from `/units` will need to switch to `units`; the legacy `/session-templates` route still preserves the alias.
