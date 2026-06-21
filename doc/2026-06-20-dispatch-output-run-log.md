# Dispatch Output Run Log

## User-visible behavior

- Dispatch step stdout/stderr from GoalManager and WorkerAgent is no longer appended to the Session MessageLog.
- Dispatch output is stored on `dispatch_runs[].steps[].output` as run history.
- MessageLog remains for actual AIZE messages, such as user input, Session capability records, Goal completion state records, explicit user-console replies, and explicit remote handoff messages.
- Existing persisted MessageLog entries created before this change are left intact as history.

## Files touched

- `src/new_aize/store.py`
- `tests/test_cli.py`

## Verification

- `python3 -m py_compile src/new_aize/*.py`
- `python3 -m unittest discover -s tests -q`

## Remaining risk

Old state files may still display previous stdout/stderr MessageLog entries until they are manually pruned or naturally fall outside the CLI tail limit. New dispatch runs no longer create those messages.
