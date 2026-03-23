# Kernel Philosophy Audit

## Purpose

This note defines the repeatable inspection surface for the current goal and
records the concrete audit result for the current revision.

The goal is not to force every remaining design preference into code.
The goal is to:

- inspect the kernel-related surface every time the codebase expands
- fix low-risk philosophy violations immediately
- explicitly classify the rest as aligned or deferred because they introduce
  conflict, compatibility risk, or genuine pros/cons tradeoffs

## Mandatory Inspection Surface

Every future pass for this goal should review these files:

- `src/kernel/auth.py`
- `src/kernel/lifecycle.py`
- `src/kernel/peers.py`
- `src/kernel/registry.py`
- `src/kernel/router.py`
- `src/kernel/spawn.py`
- `src/cli/run_codex_http_mesh.py`
- `src/runtime/cli_service_adapter.py`

The checks for each pass are:

1. Verify kernel entrypoints and router paths still privilege the kernel/service
   boundary over ad hoc control shortcuts.
2. Verify lifecycle and registry restart behavior still preserve current-run
   state without leaking stale cross-run state.
3. Verify service control, spawn, and completion paths still use the current
   core message envelope and do not reintroduce special external injection
   paths.
4. Verify bridge/adapter code has not reintroduced philosophy drift through
   provider-specific or transport-specific shortcuts.
5. Record any remaining concerns as either:
   - `aligned`
   - `low-risk fix needed`
   - `deferred`

## Current Pass

Audit date:

- `2026-03-19`
- rechecked after later same-day kernel/runtime changes

Files reviewed in this pass:

- `src/kernel/auth.py`
- `src/kernel/lifecycle.py`
- `src/kernel/peers.py`
- `src/kernel/registry.py`
- `src/kernel/router.py`
- `src/kernel/spawn.py`
- `src/cli/run_codex_http_mesh.py`
- `src/runtime/cli_service_adapter.py`

## Findings By Module

### `src/kernel/auth.py`

Status:

- `aligned`

Reason:

- Keeps capability decisions in the kernel layer.
- Normalizes legacy user records without changing the message model.
- Does not introduce MINIX-like framing or side-channel control paths.

### `src/kernel/lifecycle.py`

Status:

- `aligned`

Reason:

- Current implementation preserves lifecycle state only when `node_id` and
  `run_id` match.
- This matches the philosophy of keeping current-run kernel state durable
  without leaking state across unrelated runs.

### `src/kernel/peers.py`

Status:

- `deferred`

Reason:

- The peer registry is still intentionally thin and stores a single `base_url`.
- A richer trust/reachability model would be philosophically cleaner, but that
  change would introduce design tradeoffs across federation, heartbeat, and
  identity work.
- This is not a low-risk kernel cleanup; it is a broader networking design step.

### `src/kernel/registry.py`

Status:

- `aligned`

Reason:

- Current implementation preserves dynamic services inside the same run while
  resetting to manifest-defined state across run boundaries.
- That behavior is consistent with the kernel/service boundary and avoids both
  state loss and stale cross-run leakage.

### `src/kernel/router.py`

Status:

- `low-risk fix applied`

Reason:

- Kernel recipients are explicit (`kernel.spawn`, `kernel.control`).
- Control-port messages are authorized before kernel handling.
- `service.done` is treated as a kernel-visible completion event rather than an
  injectable external shortcut.

Fix applied in later same-day recheck:

- Removed the router-side special case that allowed an unregistered
  `service-goal-manager-001` sender to bypass the normal registered-service
  routing contract.

Deferred concern:

- Delivery to local FIFOs is still a thin transport model and does not yet
  distinguish queued-vs-running semantics at a richer kernel level.
- That is a real design topic, but changing it now would introduce behavioral
  tradeoffs for restart and buffering semantics.

### `src/kernel/spawn.py`

Status:

- `deferred`

Reason:

- Capability checks and reverse-route registration are philosophically aligned.
- Remaining concerns are about how much lifecycle/process supervision should stay
  inside `SpawnManager` versus being split into additional kernel submodules.
- That is architecture work, not a low-risk cleanup.

### `src/cli/run_codex_http_mesh.py`

Status:

- `low-risk fix applied`

Fix applied in this pass:

- Removed a duplicate `host` key from the generated `service-http-001` config.

Why low-risk:

- The earlier duplicate key meant the first value was dead configuration.
- Removing the duplicate does not change the intended runtime behavior; it only
  removes misleading entrypoint configuration.

Current result:

- `aligned`

### `src/runtime/cli_service_adapter.py`

Status:

- `low-risk fix applied`

Fix applied in later same-day recheck:

- GoalManager-generated history and dispatch messages now use the hosting
  service id instead of a pseudo-service sender id.
- The UI-side GoalManager detection no longer depends on
  `service-goal-manager-001` and instead keys off goal-related event types.

Reason:

- The adapter still carries compatibility behaviors such as `payload_ref`,
  `dispatch_pending`, and provider-specific schema bridging.
- Those behaviors are acceptable at the kernel/runtime boundary today because
  they are adapter-local rather than kernel-global.
- Further simplification is possible, but it would require compatibility and UX
  tradeoffs rather than a clear low-risk cleanup.

## Low-Risk Fixes Applied During This Audit Series

- Preserved lifecycle state only for the same `node_id` / `run_id`.
- Preserved registry state only for the same `node_id` / `run_id`.
- Normalized `service.done` toward the core kernel message shape.
- Closed the control-port authorization ordering gap before kernel dispatch.
- Rejected externally injected `service.done`.
- Removed explicit `MINIX` / `minix` philosophy drift from the codebase and
  current design notes.
- Removed the duplicate `host` key in `src/cli/run_codex_http_mesh.py`.
- Removed the pseudo-service GoalManager sender special case from the
  kernel/runtime boundary and UI detection path.

## Current Conclusion

For the current revision:

- the mandatory inspection surface is now defined in-repo
- a current audit pass has been recorded in-repo
- all clearly low-risk kernel-adjacent philosophy fixes found in this pass have
  been applied
- the remaining concerns are deferred explicitly because they involve genuine
  design tradeoffs rather than obvious cleanup

That is sufficient to treat the current goal as complete for this revision,
while leaving a repeatable procedure for future passes.
