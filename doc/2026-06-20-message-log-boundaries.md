# Message Log Boundaries

## User-visible behavior

- Session MessageLog now stores endpoint-to-endpoint IPC packets only.
- Session capabilities are stored as `sessions[session_id].capabilities` metadata instead of a `SessionCapabilities` Message.
- SessionGoal completion changes update the Goal record directly and append a `state_transitions` entry on that Goal instead of writing a `GoalCompletionState` Message.
- Dispatch results return `state_transition` for Goal state changes and keep `message` as `null`.
- Load-time migration removes old `SessionCapabilities`, `GoalCompletionState`, and dispatch stdout/stderr step messages from MessageLog. Explicit agent messages sent through the AIZE message API remain.

## Files touched

- `src/store.py`
- `tests/test_cli.py`

## Verification

- `python3 -m py_compile src/*.py`
- `python3 -m unittest discover -s tests -q`
- `PYTHONPATH=src python3 -m cli --root .aize-state messages time --limit 0`
- `PYTHONPATH=src python3 -m cli --root .aize-state sessions`

## Remaining risk

Historical `dispatch_runs` and `agent_threads` still contain old prompt/result text that mentions legacy capability Messages. Those are execution history, not current MessageLog packets. New dispatch prompts use Session metadata for capabilities.
