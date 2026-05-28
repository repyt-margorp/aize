# Provider Fallback After Panic

## User-visible behavior

Sessions are no longer pinned to a failed provider when the bound service has entered panic. If a Claude service fails authentication for a session, normal worker dispatch and GoalManager lifecycle dispatch can skip that service and lease another available provider from the session priority order.

## Files touched

- `src/runtime/cli_service_adapter.py`
  - Filters panic-blocked services out of worker dispatch candidate pools.
  - Re-selects a GoalManager service during startup queued recovery when the persisted queued service is panic-blocked.
  - Treats recent provider-wide fatal errors such as login or usage-limit failures as unavailable for new dispatch candidates.
- `src/runtime/session_lifecycle.py`
  - Skips panic-blocked services when choosing GoalManager lifecycle review workers.
  - Applies the same provider-wide fatal error skip for lifecycle GoalManager routing.

## Verification

- `PYTHONPATH=./src python3 -m py_compile src/runtime/cli_service_adapter.py src/runtime/session_lifecycle.py`

## Remaining risk

Explicit session settings that restrict a session to only one provider can still exhaust available candidates if that provider is blocked. Sessions already failed before this change need a new reconcile/restart pass to pick a fresh service.
