# Entrance SendToEntrance Render Dedupe

## Behavior changed

- Entrance chat now suppresses repeated visible chat items that have the same role, kind, text, and timestamps within five seconds.
- This covers the SendToEntrance reconciliation path where the same persisted user message can arrive through realtime/polling with different transient metadata and no stable message id.
- Existing message-id based merge behavior remains unchanged, and repeated identical messages outside the short duplicate window still render as separate turns.

## Files touched

- `src/runtime/html_renderer.py`
- `tests/test_entrance_page.py`
- `.temp/entrance_sendto_static_verify.js`

## Verification

- `PYTHONPATH=./src python3 -m unittest tests.test_entrance_page.EntrancePageTests.test_entrance_page_renders_chat_polling_surface tests.test_entrance_page.EntrancePageTests.test_communication_history_prefers_single_interactive_reply_over_duplicate_agent_event tests.test_entrance_page.EntrancePageTests.test_entrance_ui_history_collapses_provider_replay_without_agent_role_metadata -v`
- `NODE_PATH=$(npm root -g 2>/dev/null) PYTHONPATH=./src node .temp/entrance_sendto_static_verify.js`

## Browser verification

Headless Chrome opened the rendered Entrance page with a mocked backend. The backend accepted one SendToEntrance POST, then returned two copies of the sent user text one second apart without a message id. The DOM rendered the submitted text exactly once, kept the earlier user and agent messages visible, cleared the textarea and attachment chip, preserved `communication_target: "entrance"`, and showed the expected status badges: `Goal Active`, `Goal In Progress`, `Runtime Idle`, `All Clear`.

Artifacts:
- `.temp/entrance-sendto-browser-verify-current.html`
- `.temp/entrance-sendto-browser-verify-current.dom`
- `.temp/entrance-sendto-browser-verify-current.png`

## Residual risk

The renderer intentionally treats identical role/kind/text items inside a five-second window as duplicates. If a user intentionally sends the exact same Entrance text twice inside that window and the backend omits message ids for both entries, the UI may collapse them into one visible bubble; the current send guard and message-id path make that unlikely for normal SendToEntrance usage.
