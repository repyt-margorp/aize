Behavior changed: session runtime summaries now resolve `goal_audit_state` from the live per-service audit records when runtime context is available, instead of trusting any stale `goal_audit_state` field left on the stored session metadata. This keeps SessionMap and related HttpBridge list views from showing a recovered session as still being in panic.

Files touched: `src/runtime/session_view.py`, `src/runtime/cli_service_adapter.py`, `tests/test_session_listing.py`.

Verification run: `python3 -m pytest tests/test_session_listing.py -q`.

Remaining risk: callers that build session summaries without `runtime_root` and `username` still fall back to the stored session field; current HttpBridge runtime payload paths now pass the live context.
