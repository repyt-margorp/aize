# Forwarded Communication Worker Dispatch

## Behavior

- Forwarded communication prompts now still enqueue the Interactive Session WorkerAgent input.
- The worker request receives delegated-session context, so WorkerAgent can inspect or report progress for routed AIze Development child work instead of only dispatching with no pending work item.
- AIze Development routing remains driven by explicit Entrance session skills and persisted route settings, not by prompt-text worker heuristics.

## Files Touched

- `src/runtime/http_handler.py`
- `tests/test_entrance_page.py`

## Verification

- `python3 -m unittest tests.test_entrance_page -q`

## Remaining Risk

- `pytest` is not installed in this environment, so verification used `unittest` directly.
