# Agent Message API

## User-visible behavior

- Console input now records a per-console reply endpoint on `UserInput` messages.
- Agent stdout is no longer automatically mirrored to `UserConsole`.
- Agents can send AIze messages explicitly through `agent_api`.
- `UserConsole` messages require a valid reply endpoint; invalid routes are rejected by the Store layer.

## Files touched

- `src/agent_api.py`
- `src/agents.py`
- `src/cli.py`
- `src/envelope.py`
- `src/store.py`
- `tests/test_cli.py`

## Verification

```bash
python3 -m py_compile src/*.py
python3 -m unittest discover -s tests -q
```

Both commands passed.

## Remaining risk

- The API is currently a Python import available to external agents through runtime environment variables. A richer command parser for non-Python agents is still not implemented.
- Raw agent stdout is still stored as dispatch step output for audit/debug, but it is no longer treated as a user-facing message channel.
