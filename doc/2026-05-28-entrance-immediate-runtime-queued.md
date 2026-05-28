# Entrance Immediate Runtime Queueing

## User-visible change

Entrance and other communication-mode sessions now return a running/queued runtime state immediately when `/message` accepts a prompt. The HTTP response is flushed before the background dispatch thread starts its heavier routing work, and the UI updates the Entrance status badges optimistically, so a newly sent prompt no longer remains visibly idle while routing or worker assignment is still being prepared. The Entrance UI also renders the local outgoing message before the network request starts, so the user's own input is visible immediately on submit instead of waiting for the `/message` response.

Local history points to the slowdown being introduced across the Entrance routing changes, especially `17dd2a9` (`Fix Entrance-first routing and UI state`) and `7db70b9` (`Stabilize AIZE runtime and Entrance workflow`). Those commits made parent prompt submission perform child-session routing/materialization and broader dispatch setup on the send path. The current change removes that parent-to-child prompt forwarding from `/message`.

## Parent/Child routing policy

Parent session input stays in the parent session. Entrance or another parent communication session must not automatically copy or route a new parent prompt into an existing child session. Child sessions are independent execution contexts.

Child session state changes flow upward: when a child session completes, panics, or otherwise needs parent review, the system should notify the parent with a system message/pending input and queue the parent GoalManager in the same style as restart recovery. The child result is therefore visible to the parent, but parent session prompts and context are not propagated downward to the child unless a GoalManager explicitly creates a child-session request for that purpose.

## Local/private development routing

The public Entrance Unit definition keeps only distributable Entrance behavior. Machine-specific AIzeDevelopment routing is not stored in the public `aize-entrance` Unit file. For this local runtime, existing Entrance sessions were updated so the `canonical-development-routing` guidance and its spawned `aize-development-session` guidance are `adaptive` session skills. The private AIzeDevelopment units, including the ongoing MINIX/refactor and monitoring-style development units, remain available locally through the private `aize-development` plugin and runtime unit state.

## Files touched

- `src/runtime/http_handler.py`
  - Persists queued GoalManager runtime state at prompt acceptance for communication sessions.
  - Returns runtime status fields in the accepted `/message` JSON payload.
  - Flushes JSON acceptance before starting the heavier prompt dispatch thread.
  - Refreshes queued GoalManager runtime state with the resolved GoalManager service when dispatch planning reaches that point.
  - Stops automatic parent-prompt forwarding into routed child sessions.
- `src/runtime/goal_persist.py`
  - Added `persist_goal_manager_runtime_queued`.
- `src/runtime/html_renderer.py`
  - Entrance prompt submission renders an immediate running/queued status.
  - Entrance delays the post-send `/sessions` refresh slightly so the optimistic status is not immediately overwritten by an older summary.
  - Entrance renders the outgoing user input locally before starting the `/message` request, then merges it with server history.
  - Workspace composer applies runtime status returned by `/message`.
- `src/runtime/cli_service_adapter.py`
  - Adds a short-lived auth/session fast path so repeated explicit-session prompt submissions avoid unnecessary state-lock work.
- `plugins/aize-entrance/units/entrance/unit.json`
  - Keeps the public Entrance Unit free of machine-local AIzeDevelopment routing skills.
- `tests/test_http_handler_goal_save.py`
  - Added coverage that a communication prompt is running/queued before the dispatch thread starts.
- `tests/test_entrance_page.py`
  - Added UI string coverage for the optimistic Entrance status update and local outgoing-message render before fetch.
- `tests/test_session_template.py`
  - Added coverage that the public Entrance Unit provisions the code-based lightweight response skill without embedding the local AIzeDevelopment route.

## Verification

```bash
PYTHONPATH=./src python3 -m py_compile src/runtime/http_handler.py src/runtime/goal_persist.py src/runtime/html_renderer.py tests/test_http_handler_goal_save.py tests/test_entrance_page.py
PYTHONPATH=./src python3 -m py_compile src/runtime/cli_service_adapter.py
PYTHONPATH=./src python3 -m unittest tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_message_prompt_returns_running_before_dispatch_thread tests.test_http_handler_goal_save.HttpHandlerGoalSaveTests.test_message_prompt_persists_communication_runtime_when_dispatch_thread_runs tests.test_entrance_page.EntrancePageTests.test_entrance_page_renders_chat_polling_surface tests.test_http_dispatch
PYTHONPATH=./src python3 -m unittest tests.test_session_template.SessionTemplateLauncherTests.test_entrance_unit_provisions_code_based_interactive_skill tests.test_session_template.SessionTemplateLauncherTests.test_entrance_unit_launch_supports_multiple_instances tests.test_entrance_page.EntrancePageTests.test_entrance_page_renders_chat_polling_surface
```

## Remaining risk

This makes accepted work visible immediately and keeps top-level status consistent while dispatch prepares. Actual response latency can still depend on worker availability and provider startup time.

Live verification on `https://127.0.0.1:64123` after restart showed repeated Entrance `/message` sends returning HTTP 202 with `runtime_execution_state=running` and `goal_manager_state=queued` in roughly 0.08-0.18 seconds after the route removal and auth fast path were active.

After the local-render adjustment, the restarted `https://127.0.0.1:64123/units/entrance` page served the outgoing-message render before the `/message` fetch in the submit handler, and a live `/message` prompt returned HTTP 202 in 0.168 seconds with `runtime_execution_state=running`, `runtime_in_progress=true`, `goal_manager_state=queued`, and `agent_running=true`.
