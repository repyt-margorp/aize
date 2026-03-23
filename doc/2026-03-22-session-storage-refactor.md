# Session Storage Refactor Plan

## Background

Current Aize persistence is centered on a single shared JSON state file under `.aize-state/persistent.json`.
This is protected by file locking, so it is not an uncontrolled concurrent append model, but it remains a centralized state store with poor isolation and poor scaling characteristics.

This becomes increasingly problematic as the system moves toward:

- multi-agent participation in a single session
- GoalManager-driven feedback loops
- session-owned per-agent inboxes and per-session orchestration
- dynamic subgoal creation
- DAG relationships between sessions
- future distributed execution

## Immediate Problem Statement

The user wants session behavior to reflect the actual system model:

- a session exists primarily to satisfy a Goal
- multiple agents may respond into the same session
- GoalManager should react to agent replies via its own FIFO
- GoalManager should issue feedback to welcomed agents through session-owned agent inboxes and decide session-level Goal completion
- GoalManager should be able to spawn subgoal sessions
- session relationships should therefore support DAG structure rather than flat independent talks

The current centralized JSON store is not a good substrate for that model.

## Current Architecture Problems

### 1. Centralized persistent state

All session history, pending inputs, agent/provider session bindings, auth sessions, and goal state are co-located in one logical JSON file.

This causes:

- lock contention across unrelated sessions
- expensive whole-state rewrites
- weak isolation between sessions
- awkward recovery semantics
- poor fit for append-only event history

### 2. Session history is not a journal

Session history is currently stored as arrays embedded inside global JSON state.
That means history is not naturally:

- append-only
- tail-readable
- incrementally replayable
- independently shardable

### 3. GoalManager is modeled as logic over shared state rather than as a message consumer

The desired model is message-passing:

- agent reply arrives
- GoalManager input FIFO receives a message
- GoalManager wakes only to process that FIFO item
- GoalManager emits feedback / completion decisions / subgoal spawn requests

Current behavior is closer to centralized post-turn state inspection.

### 4. No explicit session DAG model

Subgoals require parent/child relationships and dependency tracking.
The current session model is flat and has no first-class representation for:

- parent session
- spawned subgoal session
- completion dependency
- blocked / waiting-on-child state

## Target Direction

Refactor persistence and session orchestration around the following principles.

### Principle 1: Session-scoped filesystem objects

Each session should own a directory, rather than being embedded inside one giant JSON document.

Proposed shape:

```text
.aize-state/
  sessions/
    <session_id>/
      session.json
      timeline.jsonl
      pending/
        session.jsonl
        goal_manager.jsonl
        agents/
          <agent_id>.jsonl
      services/
        <service_id>.json
      goal_manager/
        state.json
        reviews.jsonl
      dag/
        parents.json
        children.json
```

### Principle 2: Append-only logs for event history

The session timeline should be a JSONL journal.

This should contain:

- user-visible user input
- agent replies
- GoalManager feedback
- session events
- service / compact / audit events

`timeline.jsonl` should be append-only.
Derived summaries should be cached elsewhere.

### Principle 3: FIFO queues as explicit per-consumer inboxes

There should not be a single ambiguous pending-input list for every purpose.

Instead:

- session-level FIFO for user/session broadcast input
- GoalManager FIFO for GM-triggering work items
- per-agent inbox for each welcomed participant in the session

Important clarification:

- these inboxes are owned by the session, not by the global service
- the addressing key may still contain a concrete `service_id` for delivery, but the persistence boundary is the session
- the same runtime service may participate in many sessions with different roles, so queue identity must remain session-scoped
- GoalManager feedback should therefore be modeled as “session -> agent-in-session inbox”, not as “global service mailbox”

This matches the message-passing model more closely.

### Principle 4: State snapshots are derived, not canonical history

Canonical source of truth should be the append-only log plus queue files.

Mutable JSON files such as:

- `session.json`
- `services/<service_id>.json`
- `goal_manager/state.json`

should be treated as snapshots / indexes / caches for fast access, not as the primary event record.

### Principle 5: GoalManager as a service-like consumer

GoalManager should be treated as a message-driven actor for each session.

Conceptually:

1. An agent finishes a reply.
2. A GM work item is written into `pending/goal_manager.jsonl`.
3. GoalManager wakes, consumes one FIFO item, evaluates the Goal, and emits:
   - complete / incomplete judgment
   - per-agent feedback into session-owned agent inboxes
   - optional session-level continuation
   - optional subgoal creation request

### Principle 6: Session DAG support

Sessions should support parent/child edges.

Required capabilities:

- create child session from GoalManager
- record parent -> child edge
- mark parent as waiting on child completion
- aggregate child completion into parent Goal progression
- avoid cycles

This implies a session lifecycle state model that includes waiting/blocking semantics.

## Proposed Refactor Scope

### Phase 1: Storage split without behavior expansion

Goal:

- remove dependence on one global persistent JSON file for session data
- keep existing user-visible behavior as stable as possible

Tasks:

- create session directory model
- move session timeline into `timeline.jsonl`
- move pending queues into per-session queue files
- move per-service session metadata into per-session service files
- leave auth storage separate for now

### Phase 2: Queue semantics cleanup

Goal:

- make session FIFO / GoalManager FIFO / session-owned per-agent inbox explicit

Tasks:

- replace generic `pending_inputs` with named inboxes
- define enqueue/dequeue APIs by consumer
- ensure dispatchers consume only their own queue
- ensure FIFO visibility in HTTPBridge

### Phase 3: GoalManager message-passing model

Goal:

- make GoalManager react to queue events rather than shared-state polling

Tasks:

- emit GM work item when agent turn completes
- process one GM work item at a time
- persist GM review result as event + state snapshot
- emit feedback to one or more session-owned agent inboxes

### Phase 4: Session DAG / subgoal support

Goal:

- allow GoalManager to create child sessions and wait on them

Tasks:

- parent/child edge storage
- session dependency states
- child completion propagation
- guard against cycles

## Naming / Concept Constraints

The refactor should stay aligned with the MINIX-like philosophy already adopted in the codebase.

Guidelines:

- prefer `message`, `session`, `service`, `process`, `node`
- treat FIFO/inbox constructs as message-passing boundaries
- prefer `agent-in-session inbox` over `service-global mailbox` as the conceptual model
- avoid reintroducing centralized router-owned conversation state
- keep transport and persistence separate

## Key Open Design Decisions

### A. Auth storage split

Question:

- should auth remain in a centralized store while session data moves to per-session directories?

Tentative answer:

- yes, at least initially

### B. Queue file format

Question:

- JSONL append-only files, file-per-message objects, or directory spool?

Tentative answer:

- JSONL append-only plus offset/state cursor for first implementation

### C. Session snapshot rebuild

Question:

- how much state should be reconstructible from journal replay?

Tentative answer:

- session timeline and queues are canonical
- snapshots may be rebuilt if missing or stale

### D. GoalManager concurrency model

Question:

- one GM worker globally or logical per-session GM actor?

Tentative answer:

- logical per-session GM actor backed by shared runtime workers

## First Implementation Steps

1. Introduce a new session filesystem layout under `.aize-state/sessions/<session_id>/`.
2. Add storage adapter APIs for:
   - append timeline entry
   - read recent timeline tail
   - enqueue/dequeue session FIFO
   - enqueue/dequeue GoalManager FIFO
   - enqueue/dequeue session-owned agent inbox
3. Migrate HTTPBridge reads for one session to the new layout.
4. Migrate agent dispatch path from centralized pending arrays to session-owned agent inbox files.
5. Migrate GoalManager trigger path to explicit GM FIFO.
6. Only after storage and queue semantics are stable, add DAG/subgoal creation.

## Non-Goals For The First Refactor

- full distributed multi-node persistence
- replacing all auth/session state at once
- changing provider protocol schemas
- rewriting the entire router/kernel boundary in the same step

## Notes

Before starting this refactor, the session labeled `HTTPBRIDGEのUIのページ移り変わりが重い` was manually stopped by:

- setting its Goal inactive
- releasing its bound service
- terminating the currently running `service-claude-001` process bound only to that session

This was done to prevent the session from continuing to mutate state during the storage redesign.
