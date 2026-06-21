# UserInput Dispatch Queue

## User-visible behavior

User input sent to a Session now makes that Session dispatchable again.

When a `UserInput` Message is appended:

- the latest Goal in the Session is marked `incomplete`;
- if the Session has no Goal, a default reply Goal is created;
- a `GoalCompletionState` Message records the transition and reason;
- a priority `dispatch_queue` entry is created with user-input priority;
- in the interactive console, `send` starts a one-shot background dispatch
  worker for the current Session and returns the prompt immediately.

When `GoalManager` keeps a Goal incomplete, the state transition is still
recorded as a `GoalCompletionState` Message and a low-priority retry queue entry
is scheduled with `available_after`. This keeps the incomplete state visible in
the queue without tight-looping on the same Goal.

This fixes the case where a Session had a complete Goal, received another user
message, and then never dispatched because the Goal remained complete.

Existing state is reconciled on load: older queued `UserInput` Messages that do
not yet reference a reprocess Goal are converted into incomplete Goal state and
queued dispatch work.

## Files touched

- `src/new_aize/store.py`
  - Added persistent `dispatch_queue` state.
  - Added centralized Goal completion-state changes with reason Messages.
  - Added queued UserInput reconciliation for older state.
  - Added user-input priority dispatch enqueueing.
  - Added delayed low-priority retry queue entries for GoalManager incomplete
    results.
  - Added process-level locking around UserInput append and dispatch lease
    acquisition/commit paths.
  - Split dispatch locking so long Agent subprocess execution does not hold the
    state lock and block new UserInput.
  - Stopped stable read commands from rewriting state during queue
    reconciliation.
  - Changed state saves to use per-process unique temporary files.
- `src/new_aize/cli.py`
  - Added `dispatch-queue`.
  - Added `dispatch-worker` for foreground automatic queue processing.
  - Made interactive console `send` start a scoped background dispatch worker
    after recording input, instead of running Agent dispatch synchronously.
  - Detached the one-shot background worker from the console process so the
    prompt can return immediately while Agent dispatch continues.
  - Added a console poller that prints new `UserConsole` Messages for the
    selected Session while the prompt remains open.
- `src/new_aize/agents.py`
  - Added `AIZE_GOAL_REASON` output in local and disabled external-provider
    GoalManager paths.
- `tests/test_cli.py`
  - Added coverage for reopening a complete Goal with UserInput.
- `README.md`
- `docs/minimal-architecture.md`

## Verification

- `python3 -m py_compile src/new_aize/*.py`
- `python3 -m unittest discover -s tests -q`
- Unit coverage starts `dispatch-worker` first, then appends UserInput from a
  separate CLI process and verifies the worker dispatches that new queue entry.
- Unit coverage verifies console `send` returns after queueing background
  dispatch and dispatches the current Session before older queued work in other
  Sessions.
- Unit coverage verifies read commands do not rewrite stable state.
- Unit coverage verifies UserInput can be appended while a slow Agent dispatch
  is running.
- Smoke coverage verified `send` is followed by another console command without
  blocking, while the background worker resolves the queued Session Goal.
- Existing `.new-aize-state` `time` queue was dispatched with the real Codex
  provider and the Goal is now complete.
- Unit coverage verifies that a GoalManager incomplete result leaves a delayed
  retry queue entry and a reason-bearing `GoalCompletionState` Message.
- CLI smoke test with external Agent execution disabled:
  create `time`, complete `reply to the user`, send another UserInput, confirm
  the Goal transitions complete -> incomplete -> complete and dispatches again.
- Existing `.new-aize-state` inspection:
  `dispatch-queue time` showed the queued `UserInput` reconciled into a
  priority 100 queue entry.

## Remaining risk

`dispatch-worker` is a foreground process. A later service-manager layer can
run it as a daemon without changing the persistent `dispatch_queue` model.
