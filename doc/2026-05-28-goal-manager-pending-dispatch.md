# Goal Manager Pending Dispatch Repair

## User-visible behavior

Active in-progress sessions queued for GoalManager review are no longer dropped as no-op dispatches when their work is present only in the GoalManager state snapshot.

## Files touched

- `src/runtime/agent_service.py`
  - Includes GoalManager pending inputs in the `dispatch_pending` pre-check for `goal_manager_review`.
  - Repairs the GoalManager pending input queue from `goal_manager/state.json` `pending_work_items` before drain when the queue file is empty.
- `src/runtime/cli_service_adapter.py`
  - Re-dispatches stale startup `queued` GoalManager sessions when `pending_work_items` exist but the runtime has no live dispatch in flight.
  - Rehydrates the GoalManager pending input queue from persisted `pending_work_items` before sending the startup dispatch.

## Verification

- `PYTHONPATH=./src python3 -m py_compile src/runtime/agent_service.py`
- `PYTHONPATH=./src python3 -m py_compile src/runtime/cli_service_adapter.py`

## Remaining risk

Continuous communication sessions can still be active in-progress without immediate generic GoalManager dispatch when they are resident/listening sessions with no actionable user work. Panic-blocked sessions still require panic recovery rather than normal dispatch.
