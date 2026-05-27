# Entrance SendToEntrance SSE Dedupe

## Behavior changed

- Entrance now treats realtime outgoing user-message events as invalidation signals and refreshes from `/messages` instead of inserting those event payloads directly into the live chat buffer.
- This prevents one SendToEntrance message from rendering once from SSE replay and again from the authoritative message poll when the two payloads have different transient metadata.

## Files touched

- `src/runtime/html_renderer.py`
- `tests/test_entrance_page.py`

## Verification

- `python3 -m unittest tests.test_entrance_page -q`
- `python3 .temp/verify_entrance_sendto_dedupe.py`

## Residual risk

- If a backend emits only a realtime outgoing message and `/messages` never returns it, the Entrance page waits for the normal polling path and may not show that user bubble. The current SendToEntrance path persists submitted messages before they are visible, so `/messages` is the correct source of truth.
