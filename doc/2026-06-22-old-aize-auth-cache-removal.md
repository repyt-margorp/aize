# Old AIze auth cache removal

## User-visible behavior

- Removed old AIze archive files that contained authentication, password,
  token, secret, or credential-handling code paths.
- Added `2026-06-20-old-aize/README.md` noting that the archive is partial and
  credential-handling files were intentionally removed.
- The active CLI runtime is unchanged.

## Files touched

- `2026-06-20-old-aize/`

## Verification

- Searched the old archive for password/token/auth/secret style terms after
  deletion.
- `PYTHONPATH=src python3 -m py_compile src/*.py`
- `PYTHONPATH=src python3 -m unittest discover -s tests -q`

## Remaining risk

- This removes files from the current main tree. Already-pushed historical Git
  objects may still exist on GitHub until unreachable objects are pruned by the
  host.
