# Entrance Non-Delegated Ack Removal

## Behavior Changed

Entrance no longer writes a visible synthetic reply for ordinary `Send to Entrance` submissions. Non-delegated prompts now appear as the user's submitted message and then wait for real runtime or agent output. Explicit delegation still gets a visible route status such as `Routed to Development Unit...`.

## Cause

The old immediate acknowledgement existed in `_communication_immediate_ack_text()` as a fallback string even when Entrance had not delegated to another session. Since every `Send to Entrance` request is already addressed to Entrance, that fallback added redundant UI noise and looked like a fake agent response.

## Files Touched

- `src/runtime/http_handler.py`
- `tests/test_entrance_page.py`

## Verification

- `python3 -m unittest tests.test_entrance_page.EntrancePageTests.test_entrance_immediate_ack_does_not_claim_agent_activity_without_state tests.test_entrance_page.EntrancePageTests.test_entrance_delegated_ack_preserves_explicit_route_status`

## Residual Risk

This intentionally removes only the non-delegated synthetic acknowledgement. Users still see the local form status after submit, and the persisted timeline still records the outbound prompt.
