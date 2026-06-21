# Unit Kind Removal

## User-visible behavior

- Units no longer have or display a `kind` field.
- `create-unit` now accepts only `UNIT` plus optional `--instance-policy`.
- Console `create-unit UNIT` creates a Unit without a kind argument.
- Existing state is migrated on load by removing `units[].kind`.

## Files touched

- `src/new_aize/model.py`
- `src/new_aize/store.py`
- `src/new_aize/cli.py`
- `tests/test_cli.py`
- `docs/2026-06-20-unitless-session-creation.md`

## Verification

- `python3 -m py_compile src/new_aize/*.py`
- `python3 -m unittest discover -s tests -q`
- `PYTHONPATH=src python3 -m new_aize.cli --root .new-aize-state console --username root --password root` with `units`
