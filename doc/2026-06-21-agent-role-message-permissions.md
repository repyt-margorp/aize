# Agent Role Message Permissions

## Changed

- Added explicit runtime Message recipient permissions by agent role.
- `GoalManager` may send Messages to:
  - `Session`
  - `UserConsole`
- `WorkerAgent` may send Messages to:
  - `Session`
- `WorkerAgent` cannot send user-facing console replies directly.
- `WorkerAgent` cannot delegate work to another WorkerAgent.
- Worker prompts describe WorkerAgent as a work/reporting role that reports to Session and cannot decide SessionGoal completion.
- GoalManager prompts describe GoalManager as the only role that may send user-facing console replies and request Worker work.
- Worker requests are not Messages to `agent:WorkerAgent`; they are Session Messages with `worker_request: true`.

## Completion Authority

SessionGoal `complete` / `incomplete` state is written only by Store dispatch/session state transitions. WorkerAgent output is recorded as execution output and Messages, but is not parsed as Goal completion authority. Dispatch commits completion state from GoalManager review output.

## Verification

- `PYTHONPATH=src python3 -m py_compile src/new_aize/*.py`
- `PYTHONPATH=src python3 -m unittest discover -s tests -q`
