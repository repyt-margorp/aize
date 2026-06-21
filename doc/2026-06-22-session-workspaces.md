# Session workspaces

## User-visible behavior

- Every Session now has a `workspace_path`.
- Session workspaces are created under `workspaces/sessions/` inside the
  runtime state root.
- External GoalManager and WorkerAgent processes run with the current Session
  workspace as their process working directory.
- Agents also receive `AIZE_SESSION_WORKSPACE`.

## Files touched

- `src/agents.py`
- `src/cli.py`
- `src/store_dispatch.py`
- `src/store_session.py`
- `tests/test_cli.py`
- `README.md`

## Verification

- `PYTHONPATH=src python3 -m py_compile src/*.py`
- `PYTHONPATH=src python3 -m unittest discover -s tests -q`

## Remaining risk

- Unit-level shared workspaces are not implemented yet. This change only
  creates and uses per-Session workspaces.
