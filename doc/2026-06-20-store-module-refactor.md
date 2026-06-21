# Store Module Refactor

## User-visible behavior

- No CLI behavior changed.
- `new_aize.store.Store` remains the public API used by the CLI and Agent API.
- Dispatch, MessageLog, SessionGoal, auth, and query behavior are now separated internally.

## Internal module split

- `src/new_aize/store.py`
  - State root class.
  - File locking.
  - `init`, `load`, `save`.
  - Same-day state migrations and default initialization.
- `src/new_aize/store_defs.py`
  - Shared constants.
  - `StoreError`.
  - Endpoint helpers.
  - Message payload helpers.
- `src/new_aize/store_auth.py`
  - Account creation.
  - Password hashing.
  - Authentication.
  - Account listing.
- `src/new_aize/store_session.py`
  - Unit creation.
  - Agent provider assignment.
  - Session activation.
  - Session creation.
  - SessionGoal creation/update/state transitions.
  - Session DAG links.
- `src/new_aize/store_message.py`
  - Message construction.
  - Message indexing.
  - Endpoint cursors.
  - UserInput messages.
  - Agent runtime messages.
  - Low-level send/receive.
- `src/new_aize/store_dispatch.py`
  - Dispatch queue selection.
  - Dispatch leases and run records.
  - GoalManager / WorkerAgent execution.
  - Agent thread turns.
  - Dispatch prompt rendering.
  - Remote AIZE handoff message creation.
- `src/new_aize/store_query.py`
  - Status and list/read views used by CLI.

## Cleanup

- Removed the no-op `_migrate_legacy_agent_keys` migration hook.
- Moved `_current_goal_for_session` and `_current_goals` out of dispatch and into the SessionGoal module.
- Kept `Store` as a mixin-composed class so existing imports and CLI calls continue to work.

## Verification

```bash
python3 -m py_compile src/new_aize/*.py
python3 -m unittest discover -s tests -q
```

Result: 21 tests passed.

Additional CLI smoke check:

```bash
PYTHONPATH=src AIZE_ENABLE_EXTERNAL_AGENTS=false python3 -m new_aize.cli --root "$tmp/state" init
PYTHONPATH=src AIZE_ENABLE_EXTERNAL_AGENTS=false python3 -m new_aize.cli --root "$tmp/state" create-session smoke
PYTHONPATH=src AIZE_ENABLE_EXTERNAL_AGENTS=false python3 -m new_aize.cli --root "$tmp/state" update-goal smoke "Smoke goal body" --created-by root
PYTHONPATH=src AIZE_ENABLE_EXTERNAL_AGENTS=false python3 -m new_aize.cli --root "$tmp/state" dispatch-once
PYTHONPATH=src AIZE_ENABLE_EXTERNAL_AGENTS=false python3 -m new_aize.cli --root "$tmp/state" status
```

Result: the smoke Session completed its Goal and no dispatch lease remained acquired.

## Remaining risk

- `store.py` still re-exports constants and helpers imported by existing code. A later cleanup can move external imports to `store_defs.py` directly once callers are updated.
- `store_dispatch.py` is still the largest module because it owns queueing, leasing, Agent execution, prompt rendering, and run commit logic. It can be split further into queue and prompt modules after the dispatch model stabilizes.
