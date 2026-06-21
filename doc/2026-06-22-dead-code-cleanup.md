# Dead Code Cleanup

## Changed

- Removed unused dispatch helpers left over from earlier phase-based dispatch:
  - `_set_run_current_phase`
  - `_ensure_agent_resume_token`
- Updated stale documentation that still described `GoalManagerPrecheck` / `GoalManagerCompletion`.
- Clarified that dispatch scheduling entries are indexes into Session MessageLog rather than the main work body.

## Verification

- `PYTHONPATH=src python3 -m py_compile src/*.py`
- `PYTHONPATH=src python3 -m unittest discover -s tests -q`
