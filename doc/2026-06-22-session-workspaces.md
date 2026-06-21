# Session workspaces

## User-visible behavior

- Every Session now has a `workspace_path`.
- Session workspaces are created under `workspaces/sessions/` inside the
  runtime state root.
- External GoalManager and WorkerAgent processes run with the current Session
  workspace as their process working directory.
- Agents also receive `AIZE_SESSION_WORKSPACE`.
- Every Unit now has a shared `workspace_path`.
- Unit-derived Session workspaces contain a `unit-workspace` symlink pointing to
  the Unit workspace.
- Agents for Unit-derived Sessions also receive `AIZE_UNIT_WORKSPACE`.

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

- Existing Session workspace directories may need to be inspected manually if a
  user-created file already occupies the reserved `unit-workspace` link path.
