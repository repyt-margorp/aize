# GoalManager-Mediated Worker Dispatch

## Changed

- Dispatch no longer starts `WorkerAgent` automatically for every incomplete SessionGoal.
- Dispatch starts with `GoalManagerReview`.
- `WorkerAgent` runs only when GoalManager records a Worker request Message on the Session through `agent_api.send_worker_request(...)`.
- Worker requests are Session Messages with `payload.worker_request: true`.
- Worker prompts receive the full Session MessageLog and a dispatch-feed containing the Worker request Message.
- If UserInput arrives while WorkerAgent is active, the input is also recorded on the Session as a Worker request payload.

## Rationale

User input makes the SessionGoal incomplete and wakes GoalManager. GoalManager decides whether the request can be completed by review or needs WorkerAgent execution. Concrete work is delegated by Session Message passing, not by direct Agent-to-Agent routing.

## Verification

- `PYTHONPATH=src python3 -m py_compile src/*.py`
- `PYTHONPATH=src python3 -m unittest discover -s tests -q`
