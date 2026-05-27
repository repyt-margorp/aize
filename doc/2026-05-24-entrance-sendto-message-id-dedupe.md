# Entrance SendToEntrance Message Identity Dedupe

## User-visible behavior changed

Entrance chat now treats a persisted `message_id` as the canonical identity for a submitted SendToEntrance message. If the same submitted content reaches the page through more than one ingestion shape, such as an immediate event update plus a `/messages` refresh with different timestamps, the chat renders one user bubble.

## Cause

The previous optimistic UI insertion path was already removed, so the remaining duplicate path was the client-side timeline merge. `mergeMessages` keyed entries by direction, event type, text, and timestamp. That allowed one durable SendToEntrance message to appear twice when the same message was replayed with the same `message_id` but slightly different event/fetch metadata.

## Files touched

- `src/runtime/html_renderer.py`
- `tests/test_entrance_page.py`
- `.temp/entrance_sendto_static_verify.js` for browser verification only

## Verification

- `PYTHONPATH=./src python3 -m unittest tests.test_entrance_page.EntrancePageTests.test_entrance_page_renders_chat_polling_surface tests.test_entrance_page.EntrancePageTests.test_communication_history_prefers_single_interactive_reply_over_duplicate_agent_event tests.test_entrance_page.EntrancePageTests.test_entrance_ui_history_collapses_provider_replay_without_agent_role_metadata`
- `node .temp/entrance_sendto_static_verify.js`
- Headless Chrome verification rendered the current Entrance page, submitted `sendto-dedupe-current-1779663562416`, forced `/messages` to return two copies of that submitted text with the same `message_id` and different timestamps, and observed exactly one matching chat bubble. The same pass verified the prior user message, prior agent message, upload chip clearing, enabled send button, visible controls, session id text, and status badges.

## Remaining risk

- The fix depends on submitted-message entries carrying `message_id`. Entries without a durable id still fall back to the existing direction/type/text/timestamp key so intentional repeated sends can remain visible.
