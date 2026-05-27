## Summary

Restored deterministic Entrance fallback routing to the canonical AIze Development workflow when the Entrance routing skill declares `route_when_unhandled=true`.

## User-visible behavior

Unhandled work-bearing prompts sent to Entrance now create or reuse the canonical `aize-development.bug-hunting` parent under Root, create a delegated child task under that parent, queue the prompt into that delegated session, and leave an immediate Entrance-side routing update instead of relying on GoalManager inference alone.

## Files touched

- `plugins/aize-entrance/units/entrance/unit.json`
- `src/runtime/http_handler.py`
- `tests/test_http_handler_goal_save.py`
- `tests/test_entrance_page.py`
- `tests/test_session_template.py`

## Verification

- Pending: targeted unittest coverage for Entrance routing fallback and adjacent status behavior.

## Residual risk

- The deterministic fallback now runs before interactive/worker dispatch unless the request explicitly sets `communication_target=entrance`; adjacent goal-manager and worker reporting behavior still depends on the existing agent pipeline after the delegated session is created.
