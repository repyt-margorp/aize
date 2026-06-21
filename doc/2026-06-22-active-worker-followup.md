# Active Worker follow-up dispatch

## User-visible behavior

- UserInput sent while a WorkerAgent run is active is copied into a Session
  worker-request Message for that WorkerAgent.
- The follow-up does not interrupt the running provider subprocess through
  stdin. It waits in the dispatch index until the current WorkerAgent lease is
  released.
- The next WorkerAgent dispatch resumes the same durable WorkerAgent thread and
  sees the updated Session MessageLog and dispatch feed.
- Dispatch now avoids starting another run for the same Session/role while one
  is already acquired.
- A SessionGoal is a shared Session log target. Dispatch interprets that log as
  role-specific signals:
  - UserInput on an idle or complete Session makes GoalManager runnable.
  - GoalManager incomplete output records a Session worker request and makes
    WorkerAgent runnable.
  - WorkerAgent reports to Session, which makes GoalManager runnable again.
  - UserInput while WorkerAgent is already active is recorded for WorkerAgent
    and does not make GoalManager runnable until WorkerAgent reports.

## Files touched

- `src/store.py`
- `src/store_message.py`
- `src/store_dispatch_queue.py`
- `src/store_session.py`
- `tests/test_cli.py`
- `README.md`

## Verification

- `PYTHONPATH=src python3 -m py_compile src/*.py`
- `PYTHONPATH=src python3 -m unittest discover -s tests -q`

## Remaining risk

- This is resume-based delivery, not live stdin delivery to an already-running
  Codex process.
