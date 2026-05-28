# Startup Dispatch Pool Readiness

## Behavior Changed
- HttpBridge startup reconcile now waits for configured local LLM pools to register as running before releasing stale session bindings and requeueing active sessions.
- This avoids startup races where active sessions are marked as missing their old service, but dispatch immediately fails with `no_available_*_worker` because Codex, Claude, and Gemini services have not finished registering yet.

## Files Touched
- `src/runtime/cli_service_adapter.py`

## Verification
- `PYTHONPATH=./src python3 -m py_compile src/runtime/cli_service_adapter.py`

## Remaining Risk
- If a provider pool never reaches its configured size, startup reconcile waits up to 45 seconds and then proceeds with a timeout log instead of blocking HttpBridge indefinitely.
