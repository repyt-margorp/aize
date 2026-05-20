# Entrance Routing Lineage Restoration

## Behavior changed

- The shipped Entrance launcher template keeps the canonical development routing skill available, but it no longer routes every unhandled prompt by default.
- That route still does not use prompt-tag heuristics: `allow_tag_routing` remains disabled, and new launcher sessions require an explicit Entrance decision or an explicitly persisted session-level default route before work is delegated.
- Entrance-origin development work now materializes under the canonical `AIze Development` parent rooted at `default`, with the concrete task session created beneath that parent instead of under Entrance.
- Canonical parent reuse now falls back to the registered `aize-development.bug-hunting` unit state when a shallow session snapshot does not carry enough skill metadata to identify the already-registered development parent.

## Files touched

- `plugins/aize-entrance/units/entrance/unit.json`
- `src/runtime/http_handler.py`
- `tests/test_entrance_page.py`
- `tests/test_session_template.py`
- `doc/2026-05-21-entrance-routing-lineage-restoration.md`

## Verification

- `python3 -m unittest tests.test_entrance_page.EntrancePageTests.test_matching_communication_skill_routes_launcher_template_does_not_auto_route_by_default tests.test_entrance_page.EntrancePageTests.test_materialize_launcher_route_does_not_auto_delegate_by_default tests.test_entrance_page.EntrancePageTests.test_materialize_communication_routed_child_session_prefers_registered_bug_hunting_parent_over_existing_child tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_message_prompt_routes_entrance_request_through_development_child_proxy_path tests.test_session_template.SessionTemplateLauncherTests.test_entrance_unit_provisions_code_based_interactive_skill -v`
- `python3 -m unittest tests.test_entrance_page tests.test_http_handler_goal_save tests.test_session_template -q`

## Residual risk

- Ordinary lightweight Entrance conversation still relies on the interactive handler declining non-lightweight prompts so Entrance agents can decide whether to answer, clarify, or delegate. If that handler grows broader, it could suppress intended agent handling and should keep focused regression coverage.
- The registered-unit fallback is intentionally disabled for `route_parent_scope=origin_session`, so origin-scoped Entrance instances still keep separate delegated parent trees by design.
