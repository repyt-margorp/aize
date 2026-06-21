# Single SessionGoal State

## User-visible behavior

- `graph` and session listings now show one current Goal state per Session in
  the status bracket, such as `[Active, Complete]`, `[Active, Incomplete]`, or
  `[Active, NoGoal]`.
- `update-goal` sets or updates the Session's current Goal instead of adding another active Goal to the same Session.
- If persisted state already contains multiple non-archived Goals for one Session, load-time migration keeps the newest Goal as current and archives older Goals. Queued dispatch entries for archived Goals are marked stale.
- Dispatch and status counts ignore archived Goals.
- GoalManager completion parsing now accepts both colon text and explicit XML tag forms such as `<AIZE_GOAL_STATUS>completed</AIZE_GOAL_STATUS>`.

## Files touched

- `src/store.py`
- `src/cli.py`
- `tests/test_cli.py`

## Verification

- `python3 -m py_compile src/*.py`
- `python3 -m unittest discover -s tests -q`
- `PYTHONPATH=src python3 -m cli --root .aize-state console --username root --password root` with `graph`

## Remaining risk

Existing dispatch runs still reference historical Goal IDs. Those run records are preserved as history, while normal `goals`, `status`, queueing, and graph views use only the current SessionGoal.
