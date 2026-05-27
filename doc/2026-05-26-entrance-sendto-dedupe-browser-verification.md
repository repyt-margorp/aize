# Entrance SendToEntrance Dedupe Browser Verification

## User-visible behavior

Entrance no longer renders the same SendToEntrance user content twice when one submitted message reaches the page through multiple ingestion paths. The renderer keeps the submitted user bubble visible once while preserving nearby prior user and agent bubbles.

## Cause

The duplicate display came from the client-side Entrance timeline/message merge, not from an active optimistic UI insertion. The form submit path clears the composer and waits for persisted updates, but `/messages`, SSE/event replay, or polling can return the same outgoing SendToEntrance content with different transient timestamps or with one stable `message_id` shape plus one replay shape. Without stable identity and visible-item collapse, those entries could become separate chat bubbles.

## Files touched

- `src/runtime/html_renderer.py` adds outgoing message identity via `message_id` and collapses same-role/same-text visible items that arrive within a short replay window.
- `tests/test_entrance_page.py` asserts the Entrance page contains the dedupe behavior and still renders nearby chat/status behavior.
- `.temp/verify_entrance_sendto_dedupe.py` is an ad hoc browser verification script and is intentionally runtime scratch under `./.temp/`.

## Verification

- `python3 -m unittest tests.test_entrance_page -q` passed: 46 tests.
- `python3 .temp/verify_entrance_sendto_dedupe.py` passed in headless Google Chrome. The mocked backend accepted one SendToEntrance POST, then returned three copies of the submitted text through replay shapes: two with the same `message_id` and one without. The rendered DOM contained exactly one submitted bubble, one prior user bubble, and one prior agent bubble. The textarea was empty, status remained `Input sent. Waiting for Entrance updates...`, and badges showed `Goal Active`, `Goal In Progress`, `Runtime Idle`, and `All Clear`.
- Rechecked on 2026-05-26 with `python3 -m unittest tests.test_entrance_page.EntrancePageTests.test_entrance_page_renders_chat_polling_surface tests.test_entrance_page.EntrancePageTests.test_entrance_ui_history_collapses_replayed_interactive_reply tests.test_entrance_page.EntrancePageTests.test_entrance_ui_history_collapses_provider_replay_without_agent_role_metadata tests.test_entrance_page.EntrancePageTests.test_communication_history_prefers_single_interactive_reply_over_duplicate_agent_event -q`: 4 focused tests passed.
- Rechecked on 2026-05-26 with `python3 .temp/verify_entrance_sendto_dedupe.py`: headless Chrome rendered one submitted SendToEntrance bubble, one earlier user bubble, one earlier agent bubble, an empty textarea, and the expected status badges.
- Rechecked on 2026-05-26 with `NODE_PATH=$(npm root -g 2>/dev/null) PYTHONPATH=./src node .temp/entrance_sendto_incoming_dedupe_verify.js`: headless Chrome rendered one incoming SendToEntrance bubble for two replayed incoming records sharing the same `message_id`, while preserving nearby user/agent bubbles and session/status UI.

## Remaining risk

If two intentional identical user submissions occur inside the short duplicate window and both lack durable `message_id` values, the UI may collapse them into one visible bubble. Normal SendToEntrance submissions carry a stable message id, so this risk is limited to degraded replay shapes.
