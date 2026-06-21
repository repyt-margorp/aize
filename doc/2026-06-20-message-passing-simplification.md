# Message Passing Simplification

## User-visible behavior

- AIZE messages now use a minimal envelope:
  - `message_id`
  - `from`
  - `to`
  - `payload`
  - `created_at`
- New messages no longer store top-level `session_id`, `status`, `phase`, `sender`, `recipient`, or `body`.
- Session history is provided by a separate `message_index`, not by putting scope into the message envelope.
- Receive progress is tracked by `endpoint_cursors`, not by mutating messages to `delivered`.
- CLI `messages` displays endpoint routing (`account:root -> session:time`) and payload body text.
- `new_aize.agent_api` sends messages through the same endpoint envelope.

## Design notes

The message itself is an immutable packet. Queue/running state remains in scheduler-specific structures such as `dispatch_queue` and `dispatch_runs`. Endpoint receive progress lives in `endpoint_cursors`. Session timeline membership lives in `message_index`.

This follows the MINIX-inspired split more closely:

- Message: immutable `from -> to` payload packet
- Endpoint cursor: what a receiver has consumed
- Scheduler queue: runnable Goal/Session work
- Timeline index: UI/history projection

## Files touched

- `src/new_aize/model.py`
- `src/new_aize/store.py`
- `src/new_aize/envelope.py`
- `src/new_aize/cli.py`
- `src/new_aize/agent_api.py`
- `tests/test_cli.py`

## Verification

```bash
python3 -m py_compile src/new_aize/*.py
python3 -m unittest discover -s tests -q
```

Both commands passed.

## Remaining risk

- Existing runtime state is migrated on load. The migration preserves old message bodies and essential meanings as payload fields and builds a session timeline index from legacy `session_id`.
- `dispatch_queue` still has its own `status` because that is scheduler state, not message state.
