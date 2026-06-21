# Implicit Worker dispatch from GoalManager incomplete

## User-visible behavior

- When GoalManager marks a SessionGoal incomplete and has not already written an
  explicit Worker request, the runtime records an implicit Worker request
  Message on the Session.
- That Message queues WorkerAgent work.
- WorkerAgent still reports progress/results to the Session. A WorkerAgent
  Session report queues GoalManager review, so dispatch cycles as:
  GoalManager incomplete -> WorkerAgent work -> GoalManager review.
- Explicit `send_worker_request(...)` calls remain authoritative and do not get
  duplicated by the implicit path.

## Files touched

- `src/store_dispatch.py`
- `src/store_prompts.py`
- `tests/test_cli.py`

## Verification

- `PYTHONPATH=src python3 -m py_compile src/*.py`
- `PYTHONPATH=src python3 -m unittest discover -s tests -q`

## Remaining risk

- Incomplete results now mean WorkerAgent work by default. A later scheduler
  refinement may split incomplete into worker/action-wait/user-wait classes if
  the runtime needs to model blocked or user-wait states separately.
