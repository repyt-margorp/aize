# Session Message Context For Agents

## Changed

- GoalManager and WorkerAgent dispatch prompts now receive the Session's indexed MessageLog, not only the latest unprocessed UserInput messages.
- GoalManager completion-check prompts now include `dispatch-messages-this-run`, containing Messages sent during the current dispatch run.
- The completion-check policy now tells GoalManager to inspect messages already sent in the run before sending any additional Message, and not to repeat an existing user-facing console reply or Session answer record.

## Rationale

Session is the durable place where messages sent through the Session are recorded. Agents need that Session MessageLog as context. Without run-local message context, GoalManager can answer during precheck and then answer again during completion-check because it cannot see the reply it just sent.

## Verification

- `PYTHONPATH=src python3 -m py_compile src/new_aize/*.py`
- `PYTHONPATH=src python3 -m unittest discover -s tests -q`

## Remaining Risk

This fix gives GoalManager the necessary prompt context. It does not add routing-level suppression of duplicate user-facing replies; duplicate prevention remains a GoalManager prompt responsibility.
