# Parallel dispatch Lots

## User-visible behavior

- `aize daemon` now accepts `--dispatch-lots N`.
- The daemon keeps up to N interchangeable dispatch Lots active.
- A Lot is not owned by a Session. It is only a daemon worker thread that calls
  `dispatch_once`; any free Lot may resume any dispatchable Session/role.
- `set-dispatch-lots N` changes the target Lot size in runtime state.
- A running daemon rereads that target each cycle.
- If the target is lowered below active work, existing Codex runs are not
  cancelled. The daemon lets them release and refills only up to the lower
  target.
- If the target is raised, later cycles submit work to the extra free Lots.
- `--max-dispatch-lots` caps how far one daemon process may grow.

## Files touched

- `src/cli.py`
- `src/cli_workers.py`
- `src/store.py`
- `src/store_dispatch.py`
- `src/store_query.py`
- `tests/test_cli.py`
- `README.md`

## Verification

- `PYTHONPATH=src python3 -m py_compile src/*.py`
- `PYTHONPATH=src python3 -m unittest discover -s tests -q`

## Notes

`dispatch_lot_id` is recorded on dispatch runs for observability only. It does
not affect the durable GoalManager or WorkerAgent thread identity.
