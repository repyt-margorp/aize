# Unit Session Template Schedule

## User-visible behavior

Units now carry the SessionTemplate-like fields needed for scheduled startup:

- `display_name`
- `description`
- `goal_text`
- `initial_prompt`
- `schedule`

An interval schedule can be set through `create-unit --schedule-every-hours N`.
The schedule is managed by `schedule.next_run_at`; `every_hours` is the interval
used to advance that next run timestamp after each due launch.

Running `run-scheduled-units` starts each Unit whose `schedule.next_run_at` is
due as a child Session under `root` by default.

The created Session uses:

- Unit `display_name` as the Session title.
- Unit `goal_text` as the SessionGoal body.
- Unit `initial_prompt` as a Session user-input Message when present.

After launch, `schedule.next_run_at` advances to the next future interval
boundary.

## Files touched

- `src/model.py`
- `src/store_session.py`
- `src/store.py`
- `src/cli.py`
- `tests/test_cli.py`
- `README.md`

## Verification

- `PYTHONPATH=src python3 -m py_compile src/*.py`
- `PYTHONPATH=src python3 -m unittest discover -s tests -q`
