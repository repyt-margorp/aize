# Entrance SendToEntrance Dedupe

## Behavior changed

- Entrance no longer inserts a client-only optimistic outbound chat row after `Send to Entrance` succeeds.
- The page now refreshes chat from `/messages` immediately after the send completes, so the displayed user message comes from persisted session history only.
- This avoids showing the same SendToEntrance text once from optimistic UI state and again from the persisted timeline/SSE/poll merge path.
- The submit handler also guards concurrent sends and restores the draft text/attachments if the request fails, so clearing the local composer does not become data loss.

## Files touched

- `src/runtime/html_renderer.py`
- `tests/test_entrance_page.py`
- `.temp/entrance_sendto_static_verify.js` (browser verification harness only)

## Verification

- Added a renderer regression asserting the Entrance page does not emit the optimistic `renderChat([{direction:'out'...` path and does refresh from `/messages` after send.
- Ran a headless Chrome verification against the current renderer with a mocked Entrance backend:
  - `node .temp/entrance_sendto_static_verify.js`
  - Result: one `/message` post, one rendered user bubble for the sent text, previous user and agent messages still visible, prompt cleared, send button re-enabled, upload/Enter-send/Open-session controls visible, session id visible, and Entrance state badges visible as `Goal Active`, `Goal In Progress`, `Runtime Idle`, `All Clear`.
  - Browser artifacts: `.temp/entrance-sendto-browser-verify-current.html`, `.temp/entrance-sendto-browser-verify-current.dom`, `.temp/entrance-sendto-browser-verify-current.png`.
- Re-ran the focused renderer/history regressions:
  - `PYTHONPATH=./src python3 -m unittest tests.test_entrance_page.EntrancePageTests.test_entrance_page_renders_chat_polling_surface tests.test_entrance_page.EntrancePageTests.test_entrance_legacy_optimistic_ack_is_sanitized_for_display tests.test_entrance_page.EntrancePageTests.test_entrance_ui_history_collapses_provider_replay_without_agent_role_metadata`

## Residual risk

- The message appears after the `/messages` refresh returns instead of being inserted locally before persistence is read. This is intentional to keep the Entrance chat tied to durable runtime state.
