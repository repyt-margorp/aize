# GoalManager-Mediated Worker Dispatch

## Changed

- Dispatch no longer starts `WorkerAgent` automatically for every queued SessionGoal.
- Dispatch now starts with `GoalManager` precheck.
- `WorkerAgent` runs only when `GoalManager` records a Worker request Message on the Session through `new_aize.agent_api.send_worker_request(...)`.
- The Worker prompt includes both:
  - Session user-input messages.
  - Worker-directed messages sent by GoalManager for the same dispatch run.
- If a UserInput arrives while a dispatch run is actively in `WorkerAgent` phase, the input is also recorded on the Session as a Worker request payload.
- Worker agent threads are created only when WorkerAgent actually runs.

## Rationale

User input should first make the SessionGoal incomplete and wake GoalManager. GoalManager decides whether the request can be completed by verification or needs implementation work. Concrete work is delegated to WorkerAgent by Message passing instead of implicit broadcast.

## Verification

- `PYTHONPATH=src python3 -m py_compile src/new_aize/*.py`
- `PYTHONPATH=src python3 -m unittest discover -s tests -q`

## Remaining Risk

Worker fanout records a Message for an active Worker run. A currently running external agent can only observe it if the agent process actively polls the AIZE message API while running; otherwise it becomes durable context for later processing.
