# Session Role Dispatch Design

## Target Model

AIze Session is the durable coordination object. Agents do not own shared state with each other; they communicate by writing Messages through the Session.

Core roles:

- `GoalManager`
  - reads the Session MessageLog and current SessionGoal.
  - decides SessionGoal `complete` / `incomplete`.
  - may send a user-facing reply to `UserConsole`.
  - may write a Worker request Message to `Session`.
  - should not do implementation work except minimal verification needed for completion judgment.

- `WorkerAgent`
  - reads the Session MessageLog and Worker-directed work request.
  - performs concrete work.
  - reports progress/results to `Session`.
  - does not send Messages directly to `GoalManager`.
  - does not decide SessionGoal `complete` / `incomplete`.
  - does not send user-facing console replies.

## Message Flow

When a completed Session receives UserInput:

1. UserInput is recorded to `Session`.
2. SessionGoal becomes `incomplete`.
3. `GoalManager` dispatch is queued.
4. GoalManager reads the full Session MessageLog and a dispatch-feed containing the trigger Message.
5. GoalManager either:
   - replies and marks the Goal complete, or
   - writes a Worker request to Session and keeps the Goal incomplete.

When WorkerAgent runs:

1. WorkerAgent receives the full Session MessageLog and a dispatch-feed containing the Worker request Message.
2. WorkerAgent reports progress/results to `Session`.
3. WorkerAgent's Session report queues GoalManager review.
4. GoalManager reads the Session MessageLog and decides whether the Goal is complete or needs more work.

## Scheduling Rule

For an `Active + Incomplete` Session:

- If no GoalManager is active and the Goal is not queued, GoalManager must become dispatchable.
- WorkerAgent may be active at the same time as GoalManager.
- WorkerAgent activity should not prevent GoalManager from monitoring Session state.

## Implementation State

Implemented:

- Role message recipient permissions are explicit.
- WorkerAgent can only send runtime Messages to Session.
- WorkerAgent Session reports enqueue GoalManager review.
- GoalManager Worker requests are Session Messages with `worker_request: true`; Session dispatch turns those Messages into WorkerAgent work items.
- GoalManager/Worker prompts receive Session MessageLog context.
- Completion authority remains with GoalManager dispatch commit.
- Dispatch index entries are role-specific work items.
- Role readiness entries point to one contiguous unread SessionLog range.
- Agent prompts receive both the full `session-messages` log and the role-specific `dispatch-feed`.
- `GoalManager` runs `GoalManagerReview`.
- `WorkerAgent` runs `WorkerWork`.
- The old monolithic dispatch phases were removed from the new implementation.

## Remaining Risk

This is still a minimal role model. Future role expansion should add explicit role capability records instead of branching on hidden prompt text.
