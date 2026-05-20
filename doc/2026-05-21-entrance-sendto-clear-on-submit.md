Behavior changed: the Entrance send form now sends explicit `communication_target: "entrance"` metadata, clears the textarea and pending file attachments as soon as submit starts, blocks duplicate Enter-key submits while the request is still in flight, ignores Enter during IME composition, and restores the previous text/files if the send fails.

Files touched: `src/runtime/html_renderer.py`, `tests/test_entrance_page.py`, `.temp/entrance_sendto_static_verify.js`, `.temp/entrance_sendto_clear_verify.js`.

Cause: the Entrance page submitted the same payload as a normal communication prompt, so the HttpBridge prompt path could treat SendToEntrance as eligible for cross-session communication routing instead of an Entrance-local user input. The UI also left the submitted draft in the composer until the POST completed, which made a just-sent item visually appear in two places during the send path.

Verification run:
- `python3 -m unittest tests.test_entrance_page.EntrancePageTests.test_entrance_page_renders_chat_polling_surface tests.test_entrance_page.EntrancePageTests.test_communication_history_prefers_single_interactive_reply_over_duplicate_agent_event tests.test_entrance_page.EntrancePageTests.test_entrance_ui_history_collapses_provider_replay_without_agent_role_metadata`
- `PYTHONPATH=./src node .temp/entrance_sendto_static_verify.js`
- `node .temp/entrance_sendto_clear_verify.js`

Browser verification: headless Chrome opened the rendered Entrance page through a local mock HttpBridge surface, attached `verify.txt`, submitted with Enter-send, and confirmed the composer cleared immediately, attachment chips cleared, the submitted text rendered exactly once in chat, the nearby preexisting agent message still rendered exactly once, and the POST body included `communication_target: "entrance"` with the attached filename. A second headless Chrome pass against the current renderer attached `proof.txt`, triggered a send while the request remained in flight, attempted a second Enter press, and confirmed only one `/message` POST was emitted while the textarea and attachment chips were already cleared.

Remaining risk: full `tests.test_entrance_page` currently fails in an unrelated WIP test, `test_materialize_launcher_route_reuses_origin_scoped_registered_parent_from_shallow_sessions`, which returns `None` before this change path is exercised.
