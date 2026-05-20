# Entrance acknowledgement status

## User-visible behavior

- Entrance no longer emits the hardcoded acknowledgement claiming `InteractiveAgent` and `WorkerAgent` are active in parallel when no runtime state backs that claim.
- The non-delegated immediate acknowledgement is removed; ordinary Send to Entrance submissions wait for real runtime or agent output instead of a synthetic reply.
- Historical instances of the old synthetic acknowledgement are normalized for display to `Entrance received your request.` so the removed claim does not continue appearing from existing timelines.
- Explicit delegated routing acknowledgements still name the delegated target session.
- Entrance page transient statuses now say `Input sent. Waiting for Entrance updates...` and `New Entrance message received.` instead of naming a specific agent without runtime evidence.

## Files touched

- `src/runtime/http_handler.py`
- `src/runtime/html_renderer.py`
- `tests/test_entrance_page.py`

## Verification

- `python3 -m unittest tests.test_entrance_page -q`
- `python3 -m unittest tests.test_http_handler_goal_save -q`
- Restarted with `./restart_aize_unit.sh`.
- Verified HttpBridge health on the active local HTTPS port with certificate verification disabled.
- Ran a headless Chrome DevTools Protocol browser check against `/units/entrance?session_id=8149fee8e6aeac43`; after submitting through the Entrance form, the page status was `Input sent. Waiting for Entrance updates...`, the form remained usable, and the misleading parallel-agent claim was absent from the rendered body, including older Entrance history.

## Remaining risk

- Browser verification used a temporary communication session and checked the Entrance page DOM through headless Chrome. It did not wait for a downstream model reply, because the fix is scoped to the immediate Entrance acknowledgement and nearby local status text.
