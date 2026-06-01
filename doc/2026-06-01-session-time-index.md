# Session Time Index

## User-visible behavior

Session list and SessionMap range filtering should not require reading every `session.json` before applying the selected time window.

## Change

Session metadata writes now maintain a thin time index under runtime state:

```
.aize-state/sessions/.index/updated/YYYY/MM/DD/<username>@@<session_id>.json
.aize-state/sessions/.index/by-session/<username>/<session_id>.json
```

Each index record stores only:

- `username`
- `session_id`
- `created_at`
- `updated_at`

HTTPBridge overview/session-list views now use the index to select candidate session IDs for the requested `updated_at` window, then load full `session.json` only for those candidates.

## Verification

- `PYTHONPATH=./src python3 -m py_compile src/runtime/persistent_state_pkg/_core.py src/runtime/persistent_state_pkg/conversation.py src/runtime/persistent_state_pkg/__init__.py src/runtime/http_handler.py tests/test_session_listing.py`
- `PYTHONPATH=./src python3 -m unittest tests.test_session_listing -q`
- `PYTHONPATH=./src python3 -m unittest tests.test_http_handler_goal_save -q`

## Remaining risk

The first use on an old runtime rebuilds the index once from existing sessions. Future writes maintain the index incrementally.
