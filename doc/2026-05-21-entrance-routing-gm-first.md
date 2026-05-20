Entrance now keeps communication-session prompts inside Entrance first instead of auto-forwarding them into AIze Development from the HTTP handler.

Behavior changed
- Communication-session prompt handling in `src/runtime/http_handler.py` no longer creates or forwards a delegated development session during `/message` intake.
- Entrance GoalManager and explicit session-skill execution remain responsible for deciding whether later delegation is needed.
- This removes the backend fallback that depended on prompt-routing metadata being absent or present.

Files touched
- `src/runtime/http_handler.py`
- `tests/test_http_handler_goal_save.py`
- `tests/test_entrance_page.py`

Verification
- `python3 -m unittest tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_message_prompt_keeps_entrance_request_inside_entrance_before_goal_manager_routing`
- `python3 -m unittest tests.test_entrance_page.EntrancePageTests.test_communication_prompt_dispatch_no_longer_uses_worker_text_heuristic tests.test_entrance_page.EntrancePageTests.test_communication_dispatch_plan_keeps_worker_when_prompt_is_forwarded`
- `python3 -m py_compile src/runtime/http_handler.py tests/test_http_handler_goal_save.py tests/test_entrance_page.py`

Remaining risk
- The lower-level route materialization helpers still exist for explicit skill-driven use. If another runtime path bypasses Entrance GoalManager and calls them directly, that path can still delegate by design.
