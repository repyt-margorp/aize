# Dispatch Liveness Invariant

## Required invariant

For every current SessionGoal:

```text
Session.active == true
and SessionGoal.completion_state == incomplete
implies
at least one GoalManager or WorkerAgent role is ready or running
```

`dispatch_readiness.status == acquired` is not sufficient evidence that an
Agent is running. It is covered only when a matching `dispatch_run` still has
`lease_state == acquired`.

## Enforcement point

Every `dispatch_once()` call performs liveness reconciliation while holding the
state lock, immediately before selecting work. The daemon continuously calls
`dispatch_once()`, so this is both the scheduler boundary and the watchdog.

The reconciler performs four operations:

1. Interrupt acquired runs older than the Agent execution timeout plus grace.
2. Mark acquired readiness without a matching live run as stale.
3. Rebuild role readiness from SessionLog and RoleCursor.
4. If an Active/Incomplete Goal still has no ready or running role, append a
   GoalManager-targeted `SessionLivenessRecovery` signal and rebuild readiness.

Ready work that has waited at least 10 minutes is selected oldest-first ahead
of configured Priority. Priority controls normal ordering but cannot starve a
Session indefinitely.

The signal is durable SessionLog input. It does not create a second queue or a
hidden progress flag.

## Process failure behavior

- External Agent execution has a 600-second timeout.
- Timeout and process launch errors become `AgentError`, so normal dispatch
  error handling releases ownership and leaves the Goal incomplete.
- An unexpected dispatch Lot exception is isolated to that Lot and cannot stop
  the daemon loop. The next scheduler pass performs liveness reconciliation.
- At daemon startup, only readiness backed by an actually acquired run is
  retried. Orphan acquired readiness is made stale instead of repeating old
  Worker work.

## Inspection

```bash
./aize dispatch-liveness
./aize status
```

Healthy runtime values are:

```text
gap_count = 0
orphaned_acquired_readiness_count = 0
expired_acquired_dispatch_lease_count = 0
```

This invariant guarantees recoverable scheduler ownership while the daemon is
running or after it is restarted. It cannot make progress while the OS and
service are both stopped; durability and the systemd restart policy cover that
boundary.
