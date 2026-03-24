# Goal User Response Wait

This note records the session-goal wait-for-user flow added for HTTPBridge and GoalManager.

## Behavior

- Agents can request missing user input by appending a hidden `<aize_user_response_wait>` block to `assistant_text`.
- The runtime records:
  - requested wait seconds
  - effective wait seconds
  - wait start timestamp
  - wait deadline timestamp
  - prompt text
  - source service id
  - last cleared timestamp
  - last timeout timestamp
- The effective wait is capped at 300 seconds even if the requested value is higher.
- When the user replies, the active wait is cleared but the wait record remains.
- When the deadline passes, HTTPBridge clears the active wait, appends a resume instruction, and dispatches the pending FIFO again.

## UI

- Goal panel shows the current wait status and retained timing metadata.
- Left session navigation shows a third status dot for wait state.
- Session Map cards show `User Wait`, `Wait Timed Out`, or `Wait Recorded`.

## Validation

- `python3 -m unittest tests.test_goal_manager_compact -q`

## Requirement Mapping

- Ask the user for missing information during goal execution:
  - `src/runtime/service_control.py`
  - `src/runtime/agent_service.py`
- Record wait start time and wait duration on the session goal:
  - `src/runtime/persistent_state_pkg/_core.py`
  - `src/runtime/persistent_state_pkg/conversation.py`
  - `src/runtime/goal_persist.py`
- Cap the effective wait at 300 seconds while keeping the requested value:
  - `src/runtime/persistent_state_pkg/conversation.py`
  - `src/runtime/goal_persist.py`
- Keep the recorded wait timestamps after user reply or timeout:
  - `src/runtime/persistent_state_pkg/conversation.py`
- Resume FIFO processing automatically after timeout:
  - `src/runtime/http_handler.py`
- Show wait state in HTTPBridge Goal panel:
  - `src/runtime/http_handler.py`
  - `src/runtime/html_renderer.py`
- Show wait state in the left session list and Session Map:
  - `src/runtime/http_handler.py`
  - `src/runtime/html_renderer.py`
- Keep summary views consistent across runtime paths:
  - `src/runtime/session_view.py`
