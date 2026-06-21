# Daemon scheduled dispatch

## User-visible behavior

- Added `aize daemon`.
- The daemon initializes state if needed, polls scheduled Units, starts due Unit
  Sessions, and dispatches queued Session work in one foreground process.
- Without `--max-cycles`, the daemon keeps running until stopped.
- `--max-cycles`, `--idle-timeout`, `--schedule-interval`, and
  `--dispatch-interval` make the loop controllable for tests and service
  managers.

## Files touched

- `src/cli.py`
- `src/cli_workers.py`
- `tests/test_cli.py`
- `README.md`

## Verification

- `PYTHONPATH=src python3 -m py_compile src/*.py`
- `PYTHONPATH=src python3 -m unittest discover -s tests -q`

## Remaining risk

- This is a foreground daemon loop. A systemd unit, supervisor, or shell wrapper
  should own process restart policy in production.
