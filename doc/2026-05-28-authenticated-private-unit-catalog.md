## Behavior Changed

The authenticated Unit catalog now includes private Unit packages. This makes local
resident Units such as AIze Development and AIze MINIX Refactor visible in the Unit
screen while keeping the package-level public/private visibility marker available
for non-authenticated or exported catalog surfaces.

## Cause

AIze Development is marked with `"catalog_visibility": "private"` in its Unit package
manifest. The `/units` handler is an authenticated endpoint, but it was still building
its catalog with `include_private=False`, so Entrance was visible while AIze Development
and its refactor Units were hidden from the UI.

## Files Touched

- `src/runtime/http_handler.py`
- `tests/test_unit_catalog.py`

## Verification

- `PYTHONPATH=./src python3 -m unittest tests.test_unit_catalog -q`
- `PYTHONPATH=./src python3 -m py_compile src/runtime/http_handler.py`

## Remaining Risk

Private Units are now intentionally visible to authenticated Unit UI users. Public or
unauthenticated catalog exports should continue to choose `include_private=False` when
they need a public-only view.
