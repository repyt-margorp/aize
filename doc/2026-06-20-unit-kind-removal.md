# Unit Kind Removal

## User-visible behavior

- Units no longer have or display a `kind` field.
- `create-unit` now accepts only `UNIT` plus optional `--instance-policy`.
- Console `create-unit UNIT` creates a Unit without a kind argument.
- Existing state is migrated on load by removing `units[].kind`.

## Files touched

- `src/model.py`
- `src/store.py`
- `src/cli.py`
- `tests/test_cli.py`
- `docs/2026-06-20-unitless-session-creation.md`

## Verification

- `python3 -m py_compile src/*.py`
- `python3 -m unittest discover -s tests -q`
- `PYTHONPATH=src python3 -m cli --root .aize-state console --username root --password root` with `units`
