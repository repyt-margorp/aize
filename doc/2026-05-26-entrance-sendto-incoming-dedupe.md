# Entrance SendToEntrance Incoming Dedupe

## Behavior changed

- Entrance now preserves durable `message_id` values on incoming messages delivered to HttpBridge from other services.
- The Entrance chat merge key now treats incoming `direction: "in"` messages with the same `message_id` as one rendered chat item, matching the existing submitted-user-message behavior.
- This fixes SendToEntrance replay where the same incoming content can arrive through realtime and `/messages` polling with different transient timestamps.

## Files touched

- `src/runtime/cli_service_adapter.py`
- `src/runtime/ui_history.py`
- `src/runtime/html_renderer.py`
- `tests/test_entrance_page.py`
- `.temp/entrance_sendto_incoming_dedupe_verify.js`

## Verification

- `PYTHONPATH=./src python3 -m unittest tests.test_entrance_page.EntrancePageTests.test_entrance_page_renders_chat_polling_surface tests.test_entrance_page.EntrancePageTests.test_entrance_ui_history_collapses_replayed_interactive_reply tests.test_entrance_page.EntrancePageTests.test_communication_history_prefers_single_interactive_reply_over_duplicate_agent_event tests.test_entrance_page.EntrancePageTests.test_entrance_ui_history_collapses_provider_replay_without_agent_role_metadata -v`
- `node .temp/entrance_sendto_incoming_dedupe_verify.js`
- `NODE_PATH=$(npm root -g 2>/dev/null) PYTHONPATH=./src node .temp/entrance_sendto_static_verify.js`

## Browser verification

Headless Chrome opened the current Entrance page against a mocked backend. The backend returned two incoming SendToEntrance-style messages with the same `message_id`, same text, and timestamps twelve seconds apart. The DOM rendered the incoming text exactly once while keeping an earlier user message, an earlier agent message, session id text, and the `Goal Active`, `Goal In Progress`, `Runtime Idle`, and `All Clear` badges visible.

Artifacts:
- `.temp/entrance-sendto-incoming-dedupe.html`
- `.temp/entrance-sendto-incoming-dedupe.dom`
- `.temp/entrance-sendto-incoming-dedupe.png`

## Residual risk

- Historical incoming messages written before this fix may still lack `message_id`; those continue to rely on the existing short-window visible-text dedupe.
