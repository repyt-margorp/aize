## Behavior changed

Entrance's web interface now launches a fresh Entrance session when opened without an explicit `session_id`, instead of silently reusing the unit's recorded `last_session_id`. After launch, the page rewrites its URL to include the created `session_id`, so refresh stays attached to that specific session. The unit launcher interface button now opens the Entrance UI without prebinding it to the last recorded session, which allows multiple Entrance tabs/sessions to be opened intentionally.

## Files touched

- `src/runtime/html_renderer.py`
- `tests/test_entrance_page.py`

## Verification

- `python3 -m unittest tests.test_entrance_page tests.test_session_listing`
- Headless browser check with a local mock HTTP server: two separate visits to `/units/entrance` produced `session entrance-1` and `session entrance-2`, confirming fresh-session launch and visible session binding in the rendered page.

## Remaining risk

The browser verification used a local mock HTTP surface around the rendered page rather than the full AIze runtime, so it validates the UI launch behavior and DOM-visible session binding but not unrelated runtime integration paths.
