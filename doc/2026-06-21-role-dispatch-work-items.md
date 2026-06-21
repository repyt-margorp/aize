# Role Dispatch Work Items

## User-visible behavior

Dispatch now follows role-specific work items:

- User input makes the SessionGoal incomplete and queues `GoalManager`.
- `GoalManager` performs one review run and is the only role that changes SessionGoal complete/incomplete state.
- If `GoalManager` records a Worker request on the Session, a `WorkerAgent` queue item is created.
- `WorkerAgent` reports only to `Session`.
- A `WorkerAgent` Session report queues a later `GoalManager` review.
- Each triggered work item points at a Session Message with `trigger_message_id`.
- The agent prompt includes the whole Session MessageLog plus a smaller `dispatch-feed` for the triggering Messages.

The old `GoalManagerPrecheck -> WorkerAgent -> GoalManagerCompletion` single-run pipeline was removed.

## Files touched

- `src/store_dispatch.py`
- `src/store_dispatch_queue.py`
- `src/store_message.py`
- `src/store_defs.py`
- `src/store_prompts.py`
- `src/agents.py`
- `src/cli_render.py`
- `src/agent_api.py`
- `tests/test_cli.py`

## Verification

- `PYTHONPATH=src python3 -m py_compile src/*.py`
- `PYTHONPATH=src python3 -m unittest discover -s tests -q`
- Confirmed no old dispatch phase names or old Worker request API names remain in `src` or `tests`.
- Confirmed triggered dispatch entries are not coalesced across different Session Messages.

## Remaining risk

The dispatch model is now structurally aligned, but the role/capability model is still hard-coded to `GoalManager` and `WorkerAgent`. Generalizing to arbitrary roles should be done as a separate schema change.
