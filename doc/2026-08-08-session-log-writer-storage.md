# SessionLog single-writer storage

## Goal

Replace the monolithic/generation-copy persistence path with a file-backed
SessionLog design suitable for many asynchronous GoalManager and WorkerAgent
producers. Agent processes must never write SessionLog files directly. AIze
must establish one durable total order per Session, batch physical writes, and
derive dispatch work from committed log records.

## Required invariants

- Every SessionLog record has a strictly increasing, gap-free `seq` within its
  Session.
- Concurrent producers cannot interleave or partially overwrite records.
- Agent API success means the submitted record is durably committed unless an
  explicitly weaker acknowledgement mode is introduced later.
- GoalManager and WorkerAgent dispatch only consume committed records.
- A torn final record after process or machine interruption is detected and
  discarded without losing earlier valid records.
- Dispatch readiness and runtime indexes are derived state. SessionLog remains
  authoritative and can reconstruct pending role work.
- Hot-path Message writes do not copy unrelated Sessions, runs, or logs.
- The AIze Store coordinates CLI, Daemon, and Agent API processes through one
  cross-process writer lock. Only Store code opens SessionLog files.

## Storage layout

```text
.aize-state/
  manifest.json
  state.lock
  metadata/
    accounts.json
    units.json
    sessions.json
    session_edges.json
    goals.json
    agent_profiles.json
    runtime_settings.json
  sessions/<session-id>/
    log/
      00000000000000000001-00000000000000100000.aizelog
    log.index.json
  runtime/
    endpoint_cursors.json
    dispatch_readiness.json
    agent_threads.json
    dispatch_runs.json
  artifacts/<sha256-prefix>/<sha256>.txt
```

The first implementation keeps low-volume metadata as ID-addressed atomic JSON
files. Messages are embedded directly in SessionLog records instead of being
duplicated in a growing `messages.json`. Agent transcript bodies remain
immutable, content-addressed artifacts.

## Record and commit protocol

1. Producers call the Store/Message API; they do not open SessionLog files.
2. The Store takes the existing cross-process `state.lock`, reloads current
   metadata and the target Session tail, and assigns the next `seq`.
3. Records are encoded as framed bytes: payload length, canonical JSON payload,
   and checksum. A frame is either valid in full or ignored as a torn tail.
4. Records accumulated by one logical transaction are appended with one write
   operation and committed with `fdatasync`.
5. Related small metadata/index files are written to temporary files and
   atomically renamed. SessionLog is authoritative if interruption occurs
   between log commit and derived-index replacement.
6. Startup recovery scans segment tails, truncates invalid bytes, then rebuilds
   SessionLog-derived Message and dispatch indexes when necessary.

The existing synchronous Store lock provides one logical writer across the
Daemon, CLI, and Agent API processes. Correctness does not depend on timing or
an in-memory queue, so a process interruption cannot lose an acknowledged
Message.

## Group commit policy

- Collect every record belonging to one Store transaction and append it with one
  write batch and one `fdatasync`.
- Serialize independent producer transactions with the cross-process writer
  lock; there is no unbounded memory queue.
- Return from Message APIs only after the transaction's `fdatasync` succeeds.
- Keep Goal state, UserInput, and Worker reports in the same durable mode.

## Migration

- Read the current split-generation state once while the Daemon is stopped.
- Write per-Session log segments and atomic metadata/runtime collections.
- Validate Session counts, log counts, maximum sequence numbers, goals,
  messages, requests, runs, and transcript artifact references.
- Switch `manifest.json` to the new storage format only after validation.
- Retain one permission-restricted backup until runtime verification succeeds.
- Do not retain a permanent dual-format runtime compatibility path.

## Verification gates

- Existing full test suite passes.
- Concurrent producer test writes many records to one Session and proves unique,
  gap-free sequence ordering and valid frames.
- Multi-Session test proves one Session write does not rewrite another Session's
  log.
- Torn-tail test appends partial bytes and proves recovery preserves all prior
  records and removes the invalid tail.
- Process interruption test kills a writer around commit boundaries and proves
  restart consistency.
- Performance test demonstrates Message append cost is independent of total
  historical SessionLog size and does not create generations.
- Live state migration completes, CLI reads succeed, and the systemd user Daemon
  remains active after restart.

## Progress

- [x] Record the target architecture and invariants before implementation.
- [x] Audit all current state mutation and SessionLog call paths.
- [x] Implement framed per-Session append-only logs and recovery.
- [x] Implement atomic low-volume metadata/runtime collection persistence.
- [x] Integrate committed SessionLog reads with RoleCursor and dispatch derivation.
- [x] Implement one-time generation-store migration.
- [x] Add concurrency, torn-write, isolation, migration, and performance tests.
- [x] Run the complete regression suite and static checks.
- [x] Migrate live `.aize-state` and restart/verify `aize.service`.
- [x] Commit the completed implementation and push `main` to GitHub.

## Progress log

- 2026-08-08: Design document created. Implementation has not started.
- 2026-08-08: Audited 30 Store save paths and all direct `session_logs`
  accesses. The existing global `messages` collection duplicates SessionLog
  membership and would retain write amplification, so v2 records will embed the
  complete Message envelope. Endpoint cursors and dispatch requests remain
  rebuildable runtime indexes.
- 2026-08-08: Implemented storage v2. Session records use length/CRC frames,
  per-Session sequence assignment under the cross-process writer lock,
  `fdatasync` group commit per Store transaction, atomic ID-addressed metadata,
  and immutable transcript artifacts. Normal append reads only the Session
  index and updates it incrementally; it does not rescan historical segments.
- 2026-08-08: Added concurrent producer, Session isolation, torn process write,
  incremental append, transaction group commit, and v1 Generation migration
  tests. Focused verification passes; full regression verification is pending.
- 2026-08-08: Full verification passes: 60 tests, Python compilation, clean
  whitespace check, source/package module-list parity, and direct module imports.
- 2026-08-08: Migrated live state in 4.27 seconds. Pre/post counts match exactly:
  46 Sessions, 44 Goals, 408 Messages, 724 SessionLog records, 2 Dispatch
  requests, and 295 Dispatch runs. Restarted `aize.service`; subsequent live
  activity increased Messages to 414 and runs to 301 in storage v2. CLI status
  measured about 0.09 seconds and Daemon memory was about 106 MiB.
- 2026-08-08: Finalized the verified implementation for commit and fast-forward
  push to `origin/main`.
