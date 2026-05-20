## Behavior changed

Entrance `Send to Entrance` now clears the textarea and queued attachments immediately when a send starts, blocks duplicate submits while the request is in flight, and ignores IME composition Enter so the same draft does not get resent accidentally.

## Files touched

- `src/runtime/html_renderer.py`
- `tests/test_entrance_page.py`

## Verification run

- `python3 -m pytest tests/test_entrance_page.py`

## Remaining risk

- This change is covered by render-string regression tests. Browser behavior still needs a live UI pass if the parent session requires visual confirmation before cutover.
