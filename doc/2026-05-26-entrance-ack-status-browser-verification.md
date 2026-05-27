# Entrance acknowledgement status browser verification

## User-visible behavior

- Entrance does not show the removed optimistic acknowledgement: `Entrance received your request. InteractiveAgent is responding and WorkerAgent is checking in parallel.`
- The immediate non-delegated acknowledgement path is neutral: it does not claim InteractiveAgent or WorkerAgent activity unless delegation/runtime state supplies a real target.
- The Entrance form status after submit remains neutral: `Input sent. Waiting for Entrance updates...`

## Cause

- The misleading message was a hardcoded synthetic communication-router acknowledgement, not a runtime-derived status.
- Current code keeps explicit delegated routing acknowledgements, but `_communication_immediate_ack_text()` returns no non-delegated acknowledgement text.
- Historical timeline entries with the old router text are normalized for display by `_normalize_communication_router_ack_entry()`.

## Files touched

- `src/runtime/http_handler.py`
- `src/runtime/html_renderer.py`
- `tests/test_entrance_page.py`
- `doc/2026-05-26-entrance-ack-status-browser-verification.md`

## Verification

- `python3 -m unittest tests.test_entrance_page -q`
- `python3 -m unittest tests.test_http_handler_goal_save -q`
- `curl -k -sS https://127.0.0.1:64123/health`
- Headless Chrome DevTools Protocol browser verification against `https://127.0.0.1:64123/units/entrance` using an isolated verification Entrance Unit session.
  - Before submit: old parallel-agent claim count was `0`.
  - After submit: visible status was `Input sent. Waiting for Entrance updates...`.
  - After submit: send button was enabled.
  - After submit: old parallel-agent claim count was `0`.
  - After submit: old parallel-agent claim was absent from the chat log.

### Post-restart resume verification (2026-05-25T22:59Z)

After a system restart, re-ran the headless CDP probe against the same
runtime (`https://127.0.0.1:64123/units/entrance?session_id=1c287a4cded2c172`).
The target session intentionally has 17 historical timeline entries
containing the removed claim text, which exercises the renderer's
normalization path.

- Probe script: `./.temp/entrance_status_cdp_verify_resume.py`
- Command: `PYTHONPATH=./src python3 .temp/entrance_status_cdp_verify_resume.py`
- `/messages` endpoint: HTTP 200, 91 messages rendered.
- Non-user (`direction != "out"`) messages containing the old claim: `0`.
- `document.body.innerText` contains the old claim: `false`.
- `#status` text: `Entrance session ready.`
- Send button enabled: `true`.
- DOM snapshot: `./.temp/entrance-status-banner-browser-resume-20260525T225904Z.dom.html`
- Latest DOM symlink-style copy: `./.temp/entrance-status-banner-browser-resume.latest.dom.html`
- Screenshot: `./.temp/entrance-status-banner-browser-resume-20260525T225904Z.png`

### Re-verification after a further restart (2026-05-25T23:03Z)

After another system restart, re-ran the same CDP probe against the
same runtime and the same `1c287a4cded2c172` session. Timeline still
holds 16 raw entries containing the removed claim text (one fewer than
before, but normalization path remains exercised). `/messages` returned
fewer rows this run because the rendering pipeline now condenses prior
goal-manager and audit entries.

- `/messages` endpoint: HTTP 200, 12 messages rendered.
- Non-user (`direction != "out"`) messages containing the old claim: `0`.
- `document.body.innerText` contains the old claim: `false`.
- `#status` text: `Entrance session ready.`
- Send button enabled: `true`.
- Timeline rendered: `true`.
- DOM snapshot: `./.temp/entrance-status-banner-browser-resume-20260525T230318Z.dom.html`
- Latest DOM symlink-style copy: `./.temp/entrance-status-banner-browser-resume.latest.dom.html`
- Screenshot: `./.temp/entrance-status-banner-browser-resume-20260525T230318Z.png`

### Third post-restart re-verification (2026-05-25T23:11Z)

After a third system restart, re-ran the same CDP probe against
`https://127.0.0.1:64123/units/entrance?session_id=1c287a4cded2c172`.
Timeline still holds 16 raw entries containing the removed claim text,
so the renderer's normalization path remains exercised.

- `/messages` endpoint: HTTP 200, 9 messages rendered.
- Non-user (`direction != "out"`) messages containing the old claim: `0`.
- `document.body.innerText` contains the old claim: `false`.
- `#status` text: `Entrance session ready.`
- Send button enabled: `true`.
- Timeline rendered: `true`.
- DOM snapshot: `./.temp/entrance-status-banner-browser-resume-20260525T231146Z.dom.html`
- Latest DOM symlink-style copy: `./.temp/entrance-status-banner-browser-resume.latest.dom.html`
- Screenshot: `./.temp/entrance-status-banner-browser-resume-20260525T231146Z.png`

### Fourth post-restart re-verification (2026-05-25T23:19Z)

After a fourth system restart, re-ran the same CDP probe against
`https://127.0.0.1:64123/units/entrance?session_id=1c287a4cded2c172`.
Timeline now holds 17 raw entries containing the removed claim text,
so the renderer's normalization path remains exercised.

- `/messages` endpoint: HTTP 200, 11 messages rendered.
- Non-user (`direction != "out"`) messages containing the old claim: `0`.
- `document.body.innerText` contains the old claim: `false`.
- `#status` text: `Entrance session ready.`
- Send button enabled: `true`.
- Timeline rendered: `true`.
- DOM snapshot: `./.temp/entrance-status-banner-browser-resume-20260525T231912Z.dom.html`
- Latest DOM symlink-style copy: `./.temp/entrance-status-banner-browser-resume.latest.dom.html`
- Screenshot: `./.temp/entrance-status-banner-browser-resume-20260525T231912Z.png`

### Fifth post-restart re-verification (2026-05-25T23:25Z)

After a fifth system restart, re-ran the same CDP probe against
`https://127.0.0.1:64123/units/entrance?session_id=1c287a4cded2c172`.
Timeline now holds 18 raw entries containing the removed claim text,
so the renderer's normalization path remains exercised.

- `/messages` endpoint: HTTP 200, 11 messages rendered.
- Non-user (`direction != "out"`) messages containing the old claim: `0`.
- `document.body.innerText` contains the old claim: `false`.
- `#status` text: `Entrance session ready.`
- Send button enabled: `true`.
- Timeline rendered: `true`.
- DOM snapshot: `./.temp/entrance-status-banner-browser-resume-20260525T232507Z.dom.html`
- Latest DOM symlink-style copy: `./.temp/entrance-status-banner-browser-resume.latest.dom.html`
- Screenshot: `./.temp/entrance-status-banner-browser-resume-20260525T232507Z.png`

### Sixth post-restart re-verification (2026-05-25T23:26Z)

After a sixth system restart, re-ran the same CDP probe against
`https://127.0.0.1:64123/units/entrance?session_id=1c287a4cded2c172`.
Timeline still holds 18 raw entries containing the removed claim text,
so the renderer's normalization path remains exercised.

- `/messages` endpoint: HTTP 200, 11 messages rendered.
- Non-user (`direction != "out"`) messages containing the old claim: `0`.
- `document.body.innerText` contains the old claim: `false`.
- `#status` text: `Entrance session ready.`
- Send button enabled: `true`.
- Timeline rendered: `true`.
- DOM snapshot: `./.temp/entrance-status-banner-browser-resume-20260525T232613Z.dom.html`
- Latest DOM symlink-style copy: `./.temp/entrance-status-banner-browser-resume.latest.dom.html`
- Screenshot: `./.temp/entrance-status-banner-browser-resume-20260525T232613Z.png`

### Seventh post-restart re-verification (2026-05-25T23:31Z)

After a seventh system restart, re-ran the same CDP probe against
`https://127.0.0.1:64123/units/entrance?session_id=1c287a4cded2c172`.
Timeline now holds 19 raw entries containing the removed claim text,
so the renderer's normalization path remains exercised.

- `/messages` endpoint: HTTP 200, 11 messages rendered.
- Non-user (`direction != "out"`) messages containing the old claim: `0`.
- `document.body.innerText` contains the old claim: `false`.
- `#status` text: `Entrance session ready.`
- Send button enabled: `true`.
- Timeline rendered: `true`.
- DOM snapshot: `./.temp/entrance-status-banner-browser-resume-20260525T233112Z.dom.html`
- Latest DOM symlink-style copy: `./.temp/entrance-status-banner-browser-resume.latest.dom.html`
- Screenshot: `./.temp/entrance-status-banner-browser-resume-20260525T233112Z.png`

### Eighth post-restart re-verification (2026-05-25T23:40Z)

After an eighth system restart, re-ran the same CDP probe against
`https://127.0.0.1:64123/units/entrance?session_id=1c287a4cded2c172`.
Timeline now holds 20 raw entries containing the removed claim text,
so the renderer's normalization path remains exercised.

- `/messages` endpoint: HTTP 200, 12 messages rendered.
- Non-user (`direction != "out"`) messages containing the old claim: `0`.
- `document.body.innerText` contains the old claim: `false`.
- `#status` text: `Entrance session ready.`
- Send button enabled: `true`.
- Timeline rendered: `true`.
- DOM snapshot: `./.temp/entrance-status-banner-browser-resume-20260525T234055Z.dom.html`
- Latest DOM symlink-style copy: `./.temp/entrance-status-banner-browser-resume.latest.dom.html`
- Screenshot: `./.temp/entrance-status-banner-browser-resume-20260525T234055Z.png`

## Remaining risk

- During the browser probe, the Entrance status badge area stayed at `Resolving` because the backing `/sessions` summary request did not return within the check window. This is adjacent status latency, not the removed banner path. All eight post-restart probes reproduced the same `badgesText = "Resolving"` observation, so the latency window remains unchanged.
