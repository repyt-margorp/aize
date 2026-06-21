# Session Dispatch Feed

## User-visible behavior

Dispatch now treats Session MessageLog as the authoritative work stream.

The dispatch queue is only a scheduling index. A queued item records:

- `session_id`
- `goal_id`
- `role`
- priority and lease state
- optional `trigger_message_id`

Triggered work items are not coalesced across different Session Messages. This preserves the Session log order as the source of truth while still letting the scheduler pick the next role to run.

## Agent prompt shape

Both GoalManager and WorkerAgent receive:

- `session-messages`: the full ordered Session-related MessageLog.
- `dispatch-feed`: the trigger Message or role-relevant feed selected from the Session MessageLog.

This makes the Session more than passive history: incoming Messages can enqueue the right role, and dispatch hands the relevant Session log view to that role.

## Verification

- `PYTHONPATH=src python3 -m py_compile src/new_aize/*.py`
- `PYTHONPATH=src python3 -m unittest discover -s tests -q`
