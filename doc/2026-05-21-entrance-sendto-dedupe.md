# Entrance SendToEntrance Dedupe

## Behavior changed

- Entrance no longer inserts a client-only optimistic outbound chat row after `Send to Entrance` succeeds.
- The page now refreshes chat from `/messages` immediately after the send completes, so the displayed user message comes from persisted session history only.
- This avoids showing the same SendToEntrance text once from optimistic UI state and again from the persisted timeline/SSE/poll merge path.

## Files touched

- `src/runtime/html_renderer.py`
- `tests/test_entrance_page.py`

## Verification

- Added a renderer regression asserting the Entrance page does not emit the optimistic `renderChat([{direction:'out'...` path and does refresh from `/messages` after send.
- Ran a headless Chrome verification against the current renderer with a mocked Entrance backend:
  - `node .temp/entrance_sendto_static_verify.js`
  - Result: one `/message` post, one rendered user bubble for the sent text, previous user and agent messages still visible, prompt cleared, send button re-enabled, upload/Enter-send/Open-session controls visible, session id visible, and Entrance state badges visible as `Goal Active`, `Goal In Progress`, `Runtime Idle`, `All Clear`.
  - Browser artifacts: `.temp/entrance-sendto-browser-verify-current.html`, `.temp/entrance-sendto-browser-verify-current.dom`, `.temp/entrance-sendto-browser-verify-current.png`.

## Residual risk

- The message appears after the `/messages` refresh returns instead of being inserted locally before persistence is read. This is intentional to keep the Entrance chat tied to durable runtime state.
