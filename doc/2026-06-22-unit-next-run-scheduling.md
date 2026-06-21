# Unit next-run scheduling

## User-visible behavior

- Unit schedules are now managed by `schedule.next_run_at`.
- `--schedule-every-hours N` remains the interval configuration, but due checks
  use `next_run_at <= now`.
- `create-unit` accepts `--schedule-next-run-at TIMESTAMP` for the first run.
- After a scheduled Unit launches a Session, `schedule.next_run_at` advances to
  the next future interval boundary.

## Files touched

- `src/cli.py`
- `src/store_session.py`
- `tests/test_cli.py`
- `README.md`

## Verification

- `PYTHONPATH=src python3 -m py_compile src/*.py`
- `PYTHONPATH=src python3 -m unittest discover -s tests -q`

## Remaining risk

- Only interval schedules are supported. Calendar expressions such as daily at
  a specific local wall-clock time are not implemented yet.
