# Message Passing Refactor

## Scope

This refactor removes three conflicting control paths from AIze:

1. GoalManager must record a SessionGoal completion decision through `agent_api`, not stdout markers.
2. Every Session Message must use one locked append path that validates routing and appends its SessionLog entry atomically.
3. SessionLog is the only history of SessionGoal state transitions. Goal stores current state only.

## Target Model

- `messages` stores message envelopes only: `from`, `to`, `payload`, and `created_at`.
- A `SessionLog` entry with `kind=Message` associates an envelope with exactly one Session and gives it an ordered sequence.
- Goal state transitions are `SessionLog` entries with `kind=GoalStateChanged`; `Goal` retains only its current completion state and current reason.
- `role_cursors` consume SessionLog sequence numbers. `dispatch_requests` are derived leases over those log ranges.
- GoalManager uses `agent_api.set_goal_completion_state(state, reason)`. WorkerAgent cannot call it.
- stdout/stderr remain Run diagnostics only. They have no scheduling or state-transition meaning.

## Refactor Steps

1. Add the explicit GoalManager completion API and make dispatch read the decision from SessionLog.
2. Introduce one internal Session Message append primitive; migrate user, runtime, scheduler, implicit-worker, and remote-handoff sends to it.
3. Remove `message_index` and derive per-Session message views from SessionLog.
4. Remove `Goal.state_transitions`; remove the old `dispatch_queue` / `dispatch-index` compatibility aliases while touching the surrounding paths.
5. Update focused tests for API-based decisions, routing, and canonical SessionLog history, then run the full suite.

## Invariants

- An Agent cannot impersonate another Role through a generic CLI send path.
- Only GoalManager running under an acquired dispatch lease may set a SessionGoal completion state.
- Every Message visible to a Session has exactly one Message entry in that SessionLog.
- Dispatch never depends on Agent stdout content.
