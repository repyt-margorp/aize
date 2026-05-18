# AIze Bug-Risk Audit

Date: 2026-05-09

## Scope

Repository-wide bug-risk audit of the current AIze runtime, focused on the implemented system rather than design intent. The audit emphasized areas with high churn, large control surfaces, cross-service state transitions, and recent routing/session changes.

No source changes were made.

## Methodology

1. Reviewed the implemented architecture notes in `doc/2026-03-23-current-architecture-status.md` and `doc/2026-03-24-service-boundaries-and-state-layout.md`.
2. Checked repository shape, current worktree churn, and large-module hotspots.
3. Inspected the highest-risk runtime files directly, especially `src/runtime/http_handler.py`, `src/runtime/agent_service.py`, `src/runtime/persistent_state_pkg/conversation.py`, `src/runtime/ws_peer_client.py`, `src/runtime/ws_peer_handler.py`, `src/kernel/router.py`, and `src/kernel/registry.py`.
4. Ran syntax validation with `python3 -m compileall -q src` and `python3 -m compileall -q tests`.
5. Ran the available unit suites with `python3 -m unittest`:
   - `tests.test_kernel_registry`
   - `tests.test_http_handler_goal_save`
   - `tests.test_ws_peer_client`
   - `tests.test_session_listing`
   - `tests.test_http_bind_resolution`
   - `tests.test_tls_cert`
   - `tests.test_bootstrap_service_manager`
   - `tests.test_entrance_page`
   - `tests.test_goal_manager_compact`
   - `tests.test_verify_httpbridge_ui`

Result: the exercised test suites passed, which lowers immediate breakage risk but does not eliminate architectural bug risk in the most stateful paths.

## Subsystem Ratings

Scale:
- `1` = low bug risk
- `3` = moderate bug risk
- `5` = highest current bug risk

| Subsystem | Rating | Why |
|---|---:|---|
| HTTPBridge request handling and UI dispatch | 5 | `src/runtime/http_handler.py` is 4670 lines and mixes auth, session mutation, goal state, worker dispatch, forwarding, file upload handling, and UI responses in a single handler surface. The prompt path at `src/runtime/http_handler.py:3968-4455` is especially dense and failure-prone. |
| Agent orchestration and post-turn goal flow | 4 | `src/runtime/agent_service.py` is 3615 lines and contains a large post-turn state machine that mutates history, goal-manager queues, audit state, and recovery state in one control path (`src/runtime/agent_service.py:3202-3504`). |
| Persistent conversation/session state | 4 | `src/runtime/persistent_state_pkg/conversation.py` is 1996 lines and owns session leasing, parent/child DAG state, goal revisions, response wait state, and agent contact state. Service leasing and parent/child progression are correctness-critical (`src/runtime/persistent_state_pkg/conversation.py:516-575`, `1616-1863`). |
| WebSocket peer/federation flows | 4 | The peer client and server each maintain custom protocol/auth/state machinery with threading and broad exception handling (`src/runtime/ws_peer_client.py:661-981`, `src/runtime/ws_peer_handler.py:90-792`). Coverage exists, but it is much thinner than the local HTTP/session flows. |
| Goal audit and compaction logic | 3 | The area is heavily tested, including `tests/test_goal_manager_compact.py` with 144 passing tests, which materially reduces risk. The logic still remains complex and tightly coupled to session persistence and agent orchestration. |
| Kernel router and registry | 2 | The router/registry paths are comparatively compact and structured, with targeted regression coverage for stale process updates (`src/kernel/router.py`, `src/kernel/registry.py`, `tests/test_kernel_registry.py`). |
| Boot/service-manager/TLS | 2 | Startup and TLS logic are non-trivial but smaller, and the currently available tests passed (`tests/test_bootstrap_service_manager.py`, `tests/test_http_bind_resolution.py`, `tests/test_tls_cert.py`). |

## Key Findings

### 1. Hidden prompt-text heuristics still drive cross-session routing

Severity: high

This directly conflicts with the repository routing policy in `AGENTS.md`, which says not to gate core runtime behavior on implicit text heuristics.

Concrete evidence:
- `_communication_forward_hints()` infers routing hints from user prompt text at `src/runtime/http_handler.py:124-133`.
- `_infer_communication_forward_target_session_id()` scores sessions from labels and goal text and picks a forwarding target at `src/runtime/http_handler.py:136-175`.
- The main prompt submission path invokes that heuristic before dispatch at `src/runtime/http_handler.py:4025-4031`, then appends forwarded pending input and dispatches it at `src/runtime/http_handler.py:4107-4145`.
- The behavior is explicitly locked in by tests at `tests/test_entrance_page.py:188-224`.

Risk:
- A communication session can silently redirect work into another session based on user wording rather than explicit persisted routing state.
- This is a correctness risk, not just a maintainability concern, because the wrong session may receive the user's work request while the visible session shows the prompt as accepted.

### 2. WS peer backlog recovery appears to read the wrong state source

Severity: high

The reconnect path claims to recover prompts missed while disconnected, but the implementation reads history from the local runtime state using the remote username/session id.

Concrete evidence:
- The backlog recovery comment says it scans “the remote session's recent history” at `src/runtime/ws_peer_client.py:739-744`.
- The implementation calls `get_history(runtime_root, username=remote_username, session_id=remote_session_id)` at `src/runtime/ws_peer_client.py:745-751`.

Risk:
- In the normal cross-node case, the remote session does not live in the local `.aize-state`, so recovery can no-op or inspect an unrelated local session with the same identifiers.
- This means disconnected prompts may still be lost even though the client logs “backlog_recovery”.

Test gap:
- `tests/test_ws_peer_client.py:13-182` covers proxy-session selection and provider-pool resolution, but there is no coverage for the reconnect backlog path.

### 3. HTTPBridge returns `202 Accepted` before the highest-risk dispatch path has actually succeeded

Severity: high

The prompt workflow is executed in a detached background thread, and the immediate HTTP response is optimistic even when downstream dispatch may fail.

Concrete evidence:
- Prompt submission work is delegated to `process_prompt_submission()` and launched with `threading.Thread(...).start()` at `src/runtime/http_handler.py:4455`.
- The HTTP response is sent immediately afterward with `accepted: True` and `queued: True` at `src/runtime/http_handler.py:4456-4468`.
- Broad exception handling inside the background work only logs `http.prompt_processing_failed` at `src/runtime/http_handler.py:4439-4453`.

Risk:
- User-visible acknowledgement can diverge from actual routing state.
- Errors in service leasing, worker selection, forwarded-session dispatch, or router injection can become log-only failures after the browser already received success.

### 4. Session leasing/preemption is correct enough for tests, but operationally brittle

Severity: moderate

The leasing logic holds an exclusive state lock, scans all sessions, and may revoke another session's binding immediately if priorities differ.

Concrete evidence:
- All pool-lease discovery happens under `state_lock(runtime_root)` at `src/runtime/persistent_state_pkg/conversation.py:527-575`.
- The implementation scans every user/session directory to build the lease map at `src/runtime/persistent_state_pkg/conversation.py:539-550`.
- It preempts the lowest-priority holder by deleting its `service_id` in-place at `src/runtime/persistent_state_pkg/conversation.py:563-575`.

Risk:
- The logic is O(number of sessions) for each lease decision.
- Preemption is stateful but not explicitly surfaced back to the evicted session in the same operation, which raises race and observability risk under heavier concurrency.

Counterweight:
- This area has meaningful regression coverage in `tests/test_goal_manager_compact.py:2930-3224`.

### 5. Goal-manager and post-turn orchestration remain too monolithic for their responsibilities

Severity: moderate

The current implementation works, but too many state transitions are packed into one flow.

Concrete evidence:
- `src/runtime/agent_service.py:3202-3504` combines turn completion, goal-manager queueing, service-pending-input writes, goal-manager state-file writes, dispatch, audit failure handling, parent resume, and follow-up dispatch decisions.
- Multiple broad `except Exception` blocks convert failures into panic/audit/recovery state instead of preserving stronger local invariants (`src/runtime/agent_service.py:3344-3379`, `3480-3504`).

Risk:
- Local changes in one branch of the flow can easily perturb other branches.
- Crash consistency is hard to reason about because queue writes, state-file writes, and dispatches are not performed as a single transaction.

## Recommended Next Actions

1. Remove heuristic session forwarding from the communication path and replace it with explicit persisted routing/session-target state.
2. Rework WS backlog recovery so it queries the remote peer explicitly, or drop the feature until it can be made correct.
3. Split the `HTTPBridge` prompt path into smaller units with a synchronous preflight phase:
   - validate session target
   - resolve dispatch plan
   - persist pending work
   - only then return `202`
4. Add focused tests for:
   - WS peer reconnect backlog recovery
   - failed router injection after optimistic HTTP acceptance
   - session-lease preemption visibility to the evicted session
   - communication-mode dispatch without prompt-text routing heuristics
5. Refactor the post-turn/goal-manager flow behind smaller state-transition helpers with explicit invariants per step.

## Overall Assessment

The repository is not in a failing state: the inspected unit suites passed, and the kernel/boot layers look comparatively stable. The highest current bug risk is concentrated in the application-level orchestration layer, especially `HTTPBridge`, communication-mode routing, and cross-session/peer workflows where user intent, pending inputs, and service dispatch are coupled inside large mutable code paths.
