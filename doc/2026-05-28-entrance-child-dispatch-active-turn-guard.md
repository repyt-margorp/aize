# Entrance Child Dispatch Active Turn Guard

## User-visible behavior

Entrance-created child sessions should not receive duplicate idle-reconcile dispatch while an earlier worker turn is still open. This prevents a child task from being reassigned to another provider just because the in-memory active-turn map was lost or missed an event.

## Files touched

- `src/runtime/cli_service_adapter.py`
  - Startup reconcile skips sessions whose persisted history shows an open worker turn.
  - Session runtime payload reconstructs active worker state from history when the in-memory active-turn map is empty.

## Verification

- `PYTHONPATH=./src python3 -m py_compile src/runtime/cli_service_adapter.py`

## Remaining risk

This protects against duplicate dispatch but does not complete already-open child work by itself. Existing child sessions that already received duplicate dispatch need a fresh reconcile after restart.
