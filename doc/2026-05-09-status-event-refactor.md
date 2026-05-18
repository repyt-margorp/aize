# Status Event Refactor

AIze status changes should flow as explicit runtime messages, not as UI-only polling side effects.

## Boundary

- `runtime.status_gateway` owns status payload shape and normalization.
- `runtime.status_events` owns publishing those payloads into session history and, when needed, onward to the router.
- UI code listens for status event types and updates its local projection immediately.
- Persistent session JSON remains a checkpoint for restart/resume, not the primary notification channel.

## Current Fix

Entrance `GoalInProgress` lagged because goal state changes were persisted but not broadcast as a first-class event. The runtime now emits `goal.status_changed` whenever HTTP handlers or AgentService paths change goal progress.

## Design Direction

Keep moving toward a MINIX-style structure:

- Small modules with one responsibility.
- Explicit message passing between runtime components.
- State transitions published through a gateway instead of scattered direct UI refresh assumptions.
- Journal files used for replay and recovery after the message has been emitted.

Avoid adding hidden prompt/content heuristics to status or dispatch behavior. If behavior must vary, introduce an explicit persisted setting and route from that setting.

## Follow-up Refactor

The first dispatch split is now:

- `runtime.dispatch_policy` for pure message dispatch policy: reason parsing, provider session slot selection, service-pending-only decisions, and slot agent IDs.
- `runtime.dispatch_state` for dispatch decisions that must inspect session queues or persisted service state.
- `runtime.communication_goal` for per-prompt communication session goal lifecycle policy.

`runtime.agent_service` still orchestrates provider execution, but these extracted modules make the next split possible: GoalManager review, InteractiveAgent coordination, and WorkerAgent execution can become separate message handlers instead of one large service loop.
