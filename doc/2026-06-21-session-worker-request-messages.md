# Session Worker Request Messages

## User-visible behavior

GoalManager no longer sends work directly to `agent:WorkerAgent`.

`new_aize.agent_api.send_worker_request(body)` records a Message to the current Session with:

- `payload.body`
- `payload.worker_request: true`
- `payload.worker_role: "WorkerAgent"`
- `payload.run_id`

When the Session stores that Message, the store creates a role-specific `WorkerAgent` dispatch queue entry. Worker dispatch then reads the Session MessageLog plus the triggered Worker request Message.

The dispatch queue does not carry the work body as separate state. It stores a `trigger_message_id` pointer back into the Session MessageLog. Worker and GoalManager prompts receive:

- `session-messages`: the ordered Session-related log.
- `dispatch-feed`: the triggering Session Message or Messages for this role dispatch.

## Files touched

- `src/new_aize/agent_api.py`
- `src/new_aize/store_message.py`
- `src/new_aize/store_dispatch.py`
- `src/new_aize/store_defs.py`
- `src/new_aize/store_prompts.py`
- `src/new_aize/store_dispatch_queue.py`
- `tests/test_cli.py`
- `doc/2026-06-21-session-role-dispatch-design.md`
- `doc/2026-06-21-role-dispatch-work-items.md`
- `doc/2026-06-21-agent-role-message-permissions.md`

## Verification

- `PYTHONPATH=src python3 -m py_compile src/new_aize/*.py`
- `PYTHONPATH=src python3 -m unittest discover -s tests -q`
- Confirmed `send_worker_agent_message` is no longer present in source or tests.
- Confirmed triggered dispatch entries preserve distinct Session Message triggers.
