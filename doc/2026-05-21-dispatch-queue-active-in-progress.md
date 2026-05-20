# Dispatch Queue Active/InProgress Policy

## Behavior changed

- `dispatch_pending` messages now carry `dispatch_priority` metadata derived from explicit dispatch reasons.
- The Unit runtime orders drained dispatch messages by priority before grouping them by target/session.
- Newly launched active/in-progress Unit sessions are immediately evaluated through `enqueue_goal_dispatch` with reason `session_created`.
- Restart recovery now routes every active/in-progress session through GoalManager review first, even when the previous history contains a terminal GoalManager cycle. GoalManager receives the restart system work item and decides whether to mark the goal complete, resume a worker, or create follow-up child sessions.
- Child session state changes now have a shared parent notification helper. Existing `child_session_completed` parent dispatch remains, and panic recovery now also emits `child_session_panic` to active/in-progress parents.
- PanicRecovery creation is a fixed system action on the panicked session itself: if `A -> B` and `B` panics, the system creates recovery child `C` under `B` (`A -> B -> C`) and separately queues a `child_session_panic` system message to active/in-progress parent `A`.

## Files touched

- `src/runtime/dispatch_queue.py`
- `src/runtime/http_dispatch.py`
- `src/runtime/message_builder.py`
- `src/runtime/cli_service_adapter.py`
- `src/runtime/compaction.py`
- `src/runtime/agent_service.py`
- `src/runtime/http_handler.py`
- `tests/test_http_dispatch.py`
- `tests/test_goal_manager_compact.py`
- `tests/test_http_handler_goal_save.py`
- `tests/test_service_control.py`

## Verification

- `python3 -m py_compile src/runtime/http_dispatch.py src/runtime/dispatch_queue.py src/runtime/message_builder.py src/runtime/http_handler.py src/runtime/agent_service.py src/runtime/cli_service_adapter.py src/runtime/compaction.py`
- `python3 -m unittest discover -s tests -q`

## Residual risk

- The current queue is still an in-process message queue with persistent pending-input files as the durable work source. The new priority metadata is explicit and centrally ordered at drain time, but a future larger scheduler could persist a first-class queue table if cross-process queue introspection becomes necessary.
