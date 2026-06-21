# Unit workspaces

## User-visible behavior

- Every Unit receives a shared `workspace_path` under `workspaces/units/`.
- Session workspaces remain the Agent process working directory.
- When a Session is created from a Unit, its workspace receives a
  `unit-workspace` symlink to the Unit workspace.
- External Agents receive both `AIZE_SESSION_WORKSPACE` and, for Unit-derived
  Sessions, `AIZE_UNIT_WORKSPACE`.

## Files touched

- `src/model.py`
- `src/store_session.py`
- `src/store_dispatch.py`
- `tests/test_cli.py`
- `README.md`

## Verification

- `PYTHONPATH=src python3 -m py_compile src/*.py`
- `PYTHONPATH=src python3 -m unittest discover -s tests -q`

## Remaining risk

- The `unit-workspace` path inside a Session workspace is reserved by the
  runtime. If a user manually creates a regular file there before Unit linking,
  the store raises an error rather than overwriting it.
