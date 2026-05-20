Behavior changed:
- Entrance `Send to Entrance` multipart sends now stay in the success path when `/message` accepts the upload via redirect/followed HTML response, so the composer keeps the just-sent text area and attachment list cleared instead of restoring them.
- The shared message-send acceptance check is also used by the main workspace composer so file uploads there treat redirect-style accepted responses consistently.

Files touched:
- `src/runtime/html_renderer.py`
- `tests/test_entrance_page.py`

Verification run:
- `python3 -m unittest tests.test_entrance_page.EntrancePageTests.test_entrance_page_renders_chat_polling_surface tests.test_entrance_page.EntrancePageTests.test_main_page_unit_registry_renders_launched_session_controls`
- `google-chrome --headless --disable-gpu --allow-file-access-from-files --virtual-time-budget=4000 --dump-dom file:///home/repyt/workspace/aize/.temp/entrance-upload-verify.html`
  Result block showed `{"status":"Input sent. Waiting for Entrance updates...","text":"","chips":0,"fileInputValue":"","sendDisabled":false}` after a simulated redirect-style accepted upload response.

Remaining risk:
- The browser verification used a local rendered page with stubbed network responses rather than the full live runtime, so it specifically validates the client-side send/clear behavior and redirect-acceptance path, not end-to-end server integration.
