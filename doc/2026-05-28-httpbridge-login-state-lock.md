# HttpBridge Login State Lock

## Behavior Changed

- Authenticated HttpBridge pages no longer block behind the process-wide `persistent.lock` while active Agent processes are writing or scanning session state.
- `state_lock` and `state_read_lock` are now re-entrant within a process and default to process-local locking. Set `AIZE_STATE_FLOCK=true` to restore cross-process `flock` behavior for debugging.

## Files Touched

- `src/runtime/persistent_state_pkg/_core.py`

## Verification

- `PYTHONPATH=./src python3 -m py_compile src/runtime/persistent_state_pkg/_core.py`
- Re-entrant lock smoke test with nested `state_lock` and `state_read_lock`.
- Restarted `aize.service`.
- Verified unauthenticated root responds: `https://127.0.0.1:64123/` returned 200 in about 0.05s.
- Verified login failure path responds instead of hanging: `/login` returned 401 in about 0.75s for an intentionally wrong password.
- Verified authenticated root with a valid `bridge_session` cookie returned 200 and rendered Workspace/Session content instead of the login page.

## Remaining Risk

- Cross-process `flock` is disabled by default to keep HttpBridge responsive during heavy restart recovery. Persistent JSON writes are still atomic, but concurrent writers can theoretically race on shared top-level state. Re-enable with `AIZE_STATE_FLOCK=true` if a debugging run needs strict cross-process serialization.
