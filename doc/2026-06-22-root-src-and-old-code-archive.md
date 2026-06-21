# Root src runtime and old code archive

## User-visible behavior

- The active CLI runtime now lives as flat modules directly under `./src/`.
- The CLI entrypoint is `PYTHONPATH=src python3 -m cli ...` or the installed
  `aize` command.
- The default state root is now `./.aize-state`.
- The old AIze source code is preserved under `./2026-06-20-old-aize/` for
  reference. Historical operation logs and old tests were not restored there
  because they contained live environment records rather than reusable source.

## Files touched

- `src/*.py`
- `pyproject.toml`
- `README.md`
- `tests/test_cli.py`
- `2026-06-20-old-aize/`

## Verification

- `PYTHONPATH=src python3 -m py_compile src/*.py`
- `PYTHONPATH=src python3 -m unittest discover -s tests -q`
- `PYTHONPATH=src python3 -m cli --help`

## Remaining risk

- Existing local state directories created with older defaults are not migrated
  automatically; use `--root` explicitly if an old local state root must be
  inspected.
