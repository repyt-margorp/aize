# CLI Module Refactor

## User-visible behavior

- No CLI command behavior changed.
- `python3 -m new_aize.cli` remains the command entrypoint.
- Interactive console, dispatch workers, and non-interactive JSON commands continue to use the same command names and arguments.

## Internal module split

- `src/new_aize/cli.py`
  - Argument parser.
  - Non-interactive command dispatch.
  - Module entrypoint.
- `src/new_aize/cli_render.py`
  - Human-readable console rendering.
  - JSON printing helper.
  - Message/body formatting.
  - Agent pool and graph view formatting.
- `src/new_aize/cli_console.py`
  - Login console.
  - Console message poller.
  - Background dispatch process launch.
  - Console startup dispatch bootstrap.
- `src/new_aize/cli_workers.py`
  - Foreground dispatch loop helper.
  - Polling dispatch worker helper.

## Cleanup

- Removed direct console rendering helpers from `cli.py`.
- Moved `console_body` tests to import from `cli_render.py`, matching the new internal ownership.
- Removed generated `__pycache__` directories after verification.

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
PYTHONPATH=src AIZE_ENABLE_EXTERNAL_AGENTS=false python3 -m new_aize.cli --root "$tmp/state" user-input smoke root "hello from split cli"
PYTHONPATH=src AIZE_ENABLE_EXTERNAL_AGENTS=false python3 -m new_aize.cli --root "$tmp/state" dispatch-once
PYTHONPATH=src AIZE_ENABLE_EXTERNAL_AGENTS=false python3 -m new_aize.cli --root "$tmp/state" status
```

Result: the smoke Session completed and no dispatch lease remained acquired.

## Remaining cleanup candidates

- `run_console` still has a long command handler. It can be broken into a command table once console command semantics stabilize.
- `build_parser` is still a single parser-building function. Splitting by command group would be mostly cosmetic until the command set grows.
