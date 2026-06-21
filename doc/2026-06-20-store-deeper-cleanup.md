# Store Deeper Cleanup

## User-visible behavior

- No CLI behavior changed.
- `store.Store` remains the public runtime state API.
- Constants, endpoint helpers, and payload helpers are now imported by callers from `store_defs.py` instead of being treated as `store.py` responsibilities.

## Cleanup

- Updated `cli.py` and `agent_api.py` to import constants and helpers from `store_defs.py`.
- Reduced `store.py` imports to the symbols it actually uses internally.
- Split dispatch internals further:
  - `store_dispatch.py`: dispatch lease/execution/commit flow.
  - `store_dispatch_queue.py`: dispatch index selection, retry timing, scheduling entry lifecycle.
  - `store_prompts.py`: GoalManager/WorkerAgent prompt rendering and GoalManager output parsing.
- Cleaned misleading indentation in `store_defs.py` and `store_prompts.py`.
- Updated tests to import `StoreError` from `store_defs.py`.

## Verification

```bash
python3 -m py_compile src/*.py
python3 -m unittest discover -s tests -q
```

Result: 21 tests passed.

Additional CLI smoke check:

```bash
PYTHONPATH=src AIZE_ENABLE_EXTERNAL_AGENTS=false python3 -m cli --root "$tmp/state" init
PYTHONPATH=src AIZE_ENABLE_EXTERNAL_AGENTS=false python3 -m cli --root "$tmp/state" create-session smoke
PYTHONPATH=src AIZE_ENABLE_EXTERNAL_AGENTS=false python3 -m cli --root "$tmp/state" user-input smoke root "hello"
PYTHONPATH=src AIZE_ENABLE_EXTERNAL_AGENTS=false python3 -m cli --root "$tmp/state" dispatch-once
PYTHONPATH=src AIZE_ENABLE_EXTERNAL_AGENTS=false python3 -m cli --root "$tmp/state" status
```

Result: the smoke Session completed and no dispatch lease remained acquired.

## Remaining cleanup candidates

- `cli.py` is now the largest file and mixes command parsing, console rendering, console polling, and worker loop helpers.
- `store.py` still contains same-day migration code. That can be removed later once `.aize-state` compatibility with earlier schema iterations is no longer needed.
