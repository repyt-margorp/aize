# Session Labels And Goal Bodies

## Changed

- `SessionGoal` records no longer have a `title` field. A Goal is represented by its body and completion state.
- `Session.title` is the UI-facing label for a Session.
- `start-goal` now requires `--label`; the old `--title` option is not supported.
- `update-goal` now accepts only the Goal body:

```bash
PYTHONPATH=src python3 -m new_aize.cli --root .new-aize-state update-goal SESSION "goal body" --created-by root
```

- Removed same-day state migration helpers from `Store.load()`. The new system expects state files to use the current schema while the schema is still being designed.
- Renamed endpoint normalization code away from legacy terminology.
- Agent message attachment parameters are now named `files`, and message files live under `payload.files`.

## Verification

- `PYTHONPATH=src python3 -m py_compile src/new_aize/*.py`
- `PYTHONPATH=src python3 -m unittest discover -s tests -q`

## Remaining Risk

Existing `.new-aize-state` directories created by earlier same-day schema iterations may need to be recreated instead of migrated.
