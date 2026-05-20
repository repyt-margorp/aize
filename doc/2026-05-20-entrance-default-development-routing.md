## Behavior Changed

Entrance now routes unhandled work-bearing prompts to the canonical AIze Development parent through an explicit session-skill flag instead of matching prompt text against routing tags.

## Current Inspection

- `plugins/aize-entrance/units/entrance/unit.json` defines the `canonical-development-routing` skill with `route_when_unhandled: true`, `target_template_id: aize-development.bug-hunting`, `target_label: AIze Development`, and `target_child_label: AIze Development Task`.
- `src/runtime/http_handler.py` uses that persisted skill contract for communication routing. `route_when_unhandled` routes without reading prompt keywords; tag matching is only honored when a skill explicitly sets `allow_tag_routing: true`.
- Interactive sessions with `communication_agent_enabled=true` still dispatch InteractiveAgent, WorkerAgent, and GoalManager paths from explicit session settings. The old `_interactive_prompt_needs_worker(prompt_text)` gate is absent.

## Files Touched

- `src/runtime/http_handler.py`
- `src/runtime/persistent_state_pkg/_core.py`
- `plugins/aize-entrance/units/entrance/unit.json`
- `tests/test_entrance_page.py`
- `tests/test_http_handler_goal_save.py`
- `tests/test_session_template.py`

## Verification

- `python3 -m unittest tests.test_entrance_page tests.test_http_handler_goal_save tests.test_session_template`
- `python3 -m unittest tests.test_entrance_page.EntrancePageTests.test_communication_prompt_dispatch_no_longer_uses_worker_text_heuristic tests.test_entrance_page.EntrancePageTests.test_materialize_direct_development_route_launches_canonical_parent tests.test_entrance_page.EntrancePageTests.test_materialize_communication_routed_child_session_reuses_registered_parent_for_parallel_tasks`

## Residual Risk

- Non-Entrance route skills that still depend on prompt-tag heuristics must now opt in with `allow_tag_routing=true`; that is intentional, but any existing skill that relied on implicit tag matching will need the explicit flag.
- A later rerun of `python3 -m unittest tests.test_entrance_page tests.test_http_handler_goal_save tests.test_session_template -q` failed in `tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_message_prompt_runs_interactive_session_skill_through_history_path`: the history entry expected with `direction == "out"` was missing. The routing-specific tests above still pass.
