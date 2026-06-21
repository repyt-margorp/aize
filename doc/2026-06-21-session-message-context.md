# Session Message Context For Agents

## Changed

- GoalManager and WorkerAgent dispatch prompts receive the Session's indexed MessageLog.
- Both prompts also receive a `dispatch-feed` selected from that MessageLog.
- GoalManager uses `GoalManagerReview`.
- WorkerAgent uses `WorkerWork`.

## Rationale

Session is the durable place where messages sent through the Session are recorded. Dispatch scheduling entries are only indexes into that Session log. Agents need the whole log for context and the dispatch-feed for the immediate trigger.

## Verification

- `PYTHONPATH=src python3 -m py_compile src/*.py`
- `PYTHONPATH=src python3 -m unittest discover -s tests -q`
