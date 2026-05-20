# Entrance Routing Lineage Restoration

## Behavior changed

- The shipped Entrance launcher template keeps the canonical development routing skill available, but it no longer routes every unhandled prompt by default.
- That route still does not use prompt-tag heuristics: `allow_tag_routing` remains disabled, and new launcher sessions require an explicit Entrance decision or an explicitly persisted session-level default route before work is delegated.
- `Send to Entrance` submits `communication_target=entrance` in both JSON and multipart requests, so HttpBridge keeps that prompt in the Entrance session for InteractiveAgent, WorkerAgent, and GoalManager review before any later delegation.
- Entrance prompt history now records the local Entrance target instead of rewriting the outbound row to `forward:<child-session-id>`.
- Entrance-origin development work now materializes under the canonical `AIze Development` parent rooted at `default`, with the concrete task session created beneath that parent instead of under Entrance.
- Canonical parent reuse now falls back to the registered `aize-development.bug-hunting` unit state when a shallow session snapshot does not carry enough skill metadata to identify the already-registered development parent.
- The Entrance composer clears the text, pending files, and file input after the send starts, and restores them if the send fails.
- Restart resume now resolves the startup-only recovery budget before candidate scanning, so GoalManager/restart-resume bookkeeping cannot hit an unbound local when the resume path evaluates later routing state.

## Files touched

- `plugins/aize-entrance/units/entrance/unit.json`
- `src/runtime/html_renderer.py`
- `src/runtime/http_handler.py`
- `src/runtime/compaction.py`
- `tests/test_entrance_page.py`
- `tests/test_http_handler_goal_save.py`
- `tests/test_session_template.py`
- `doc/2026-05-21-entrance-routing-lineage-restoration.md`

## Verification

- `python3 -m unittest tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_message_prompt_keeps_entrance_request_inside_entrance_before_goal_manager_routing tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_message_prompt_runs_entrance_handler_before_unhandled_development_route tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_message_prompt_with_entrance_target_stays_in_entrance_before_delegation tests.test_entrance_page.EntrancePageTests.test_entrance_page_renders_chat_polling_surface -v`
- `python3 -m unittest tests.test_entrance_page tests.test_http_handler_goal_save tests.test_session_template -q`
- `python3 -m unittest tests.test_goal_manager_compact -q`
- `python3 -m unittest tests.test_entrance_page tests.test_http_handler_goal_save tests.test_goal_manager_compact -q`

## Residual risk

- Ordinary lightweight Entrance conversation still relies on the interactive handler declining non-lightweight prompts so Entrance agents can decide whether to answer, clarify, or delegate. If that handler grows broader, it could suppress intended agent handling and should keep focused regression coverage.
- The registered-unit fallback is intentionally disabled for `route_parent_scope=origin_session`, so origin-scoped Entrance instances still keep separate delegated parent trees by design.
