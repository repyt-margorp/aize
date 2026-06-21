# Agent Pool Visibility

## User-visible behavior

- Console `graph` now shows active agent allocations per Session as `G:n,W:m`.
- `G` is the number of active GoalManager allocations for acquired dispatch runs on that Session.
- `W` is the number of active WorkerAgent allocations for acquired dispatch runs on that Session.
- Queued but not running Sessions show `queued`; acquired runs show non-zero allocation counts and `running`.
- New CLI command `agent-pool` shows current allocation counts for GoalManager and WorkerAgent.

## Files touched

- `src/cli.py`
- `tests/test_cli.py`

## Verification

- `python3 -m py_compile src/*.py`
- `python3 -m unittest discover -s tests -q`
- `PYTHONPATH=src python3 -m cli --root .aize-state agent-pool`
- Console `graph` shows `G:0,W:0` for queued but idle Sessions.
