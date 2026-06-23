# CLI Startup Dispatch Recovery

## User-visible behavior

- Opening `cli console` now checks for queued Active/Incomplete SessionGoals.
- If queued work exists and no dispatch lease is currently acquired, the console starts a detached background `dispatch-worker`.
- The startup worker polls the global dispatch index briefly and dispatches pending work until the index becomes idle.
- User `send` still starts a session-scoped background dispatch worker for the current Session.

## Recovery context

- Console startup dispatches pass a recovery context to GoalManager and WorkerAgent prompts:
  - the CLI console started or restarted;
  - persisted state may have changed;
  - agents should continue toward the current SessionGoal from current Session state.
- This recovery context is stored on the `dispatch_runs` record.
- It is not appended to the Session MessageLog, because it is runtime dispatch context rather than endpoint-to-endpoint AIze IPC.

## Files touched

- `src/cli.py`
  - added startup dispatch bootstrap;
  - added `--recovery-context` plumbing for dispatch commands;
  - made background dispatch startup support global queue workers.
- `src/store.py`
  - threads recovery context through dispatch lease metadata;
  - renders recovery context into GoalManager and WorkerAgent prompts;
  - keeps recovery context out of MessageLog.
- `tests/test_cli.py`
  - added coverage for recovery context placement;
  - added coverage for console startup dispatch of queued active incomplete Sessions;
  - updated console dispatch expectations for queued work on startup.

## Verification

```bash
python3 -m py_compile src/*.py
python3 -m unittest discover -s tests -q
```

Result: 21 tests passed.

## Remaining risk

- This is still console-startup bootstrap, not a long-running daemon supervisor.
- Multiple consoles opened at nearly the same time may each start a worker, but dispatch leases prevent duplicate execution of the same queue entry.
