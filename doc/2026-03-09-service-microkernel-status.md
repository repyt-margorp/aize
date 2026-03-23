# Service Microkernel Status

## Purpose

This note records:

- what has already been implemented
- what concepts are now stable
- what still needs to be implemented next

It is a companion to `2026-03-09-agent-mesh-runtime-plan.md`, but focuses on current state rather than broad direction.

## Current Architectural Position

The system is no longer a `tmux send-keys` experiment.

It now behaves like a small local microkernel-style runtime on Linux with these core concepts:

- `service`
- `process`
- `message`
- `port`
- `router`

Linux remains the host OS.

The runtime is still single-node in deployment, but the message model has already been prepared for later distributed routing.

## What Has Been Implemented

### 1. Layer Split

The codebase is visibly split into:

- `kernel`
- `runtime`
- `wire`
- `memory`
- `cli`

Relevant files:

- [cli/run_codex_claude_mesh.py](../cli/run_codex_claude_mesh.py)
- [kernel/router.py](../kernel/router.py)
- [runtime/cli_service_adapter.py](../runtime/cli_service_adapter.py)
- [wire/protocol.py](../wire/protocol.py)
- [memory/README.md](../memory/README.md)

### 2. Service Model

The runtime now uses `service_id` as the logical identity.

Example services:

- `service-codex-001`
- `service-claude-001`

The manifest now has:

- `node_id`
- `services`
- `routes`
- `settings`

Current manifest output:

- [manifest.json](../.agent-mesh-runtime/manifest.json)

### 3. Process Model

The runtime now separates `service` from `process`.

- `service_id`
  logical recipient / service identity
- `process_id`
  concrete running instance that currently implements the service

`process_id` is generated at adapter start and logged throughout execution.

### 4. Message Model

The runtime uses structured JSON messages.

Current message fields include:

- `message_id`
- `run_id`
- `from_node`
- `from`
- `to_node`
- `to`
- `type`
- `ts`
- `payload` or `payload_ref`
- optional `process_id`

Message construction lives in:

- [protocol.py](../wire/protocol.py)

### 5. Port Model

The runtime now uses `ports` terminology instead of `mailboxes`.

Each service has:

- `rx`
  receive port
- `tx`
  transmit port

Current local runtime layout:

- `.agent-mesh-runtime/ports/router.control`
- `.agent-mesh-runtime/ports/service-codex-001.rx`
- `.agent-mesh-runtime/ports/service-codex-001.tx`
- `.agent-mesh-runtime/ports/service-claude-001.rx`
- `.agent-mesh-runtime/ports/service-claude-001.tx`

Transport is currently FIFO-based.

### 6. Router

The router is now a node-local message switch.

Current responsibilities:

- accept control/seed messages
- accept outbound service messages
- validate static route policy
- forward to local service ports
- log message forwarding attempts
- track `service.done`
- stop when all services are done

It also now understands `node_id` enough to distinguish:

- local delivery
- remote delivery not yet implemented

If a message targets another node, the router can defer it instead of pretending it is local.

### 7. Codex / Claude Service Adapters

The runtime already runs real external services:

- Codex
- Claude

The adapter:

- reads one inbound message
- turns it into a provider prompt
- runs the backend CLI
- captures provider-native JSON output
- logs provider events
- emits one outbound message

This has been verified repeatedly by running:

```bash
python3 -m cli.run_codex_claude_mesh
```

### 8. Message Body Strategy

The runtime supports two message body modes:

- inline `payload.text`
- referenced `payload_ref`

The cutoff is configurable through manifest settings:

- `settings.inline_payload_max_bytes`

Current default:

- `4096`

If a message body is larger than the threshold, the text is stored under:

- `.agent-mesh-runtime/objects/`

and the message carries only `payload_ref`.

### 9. Logging

The runtime already logs at three important levels:

- router log
- per-service log
- provider-native event log inside each service log

Current files:

- [router.jsonl](../.agent-mesh-runtime/logs/router.jsonl)
- [service-codex-001.jsonl](../.agent-mesh-runtime/logs/service-codex-001.jsonl)
- [service-claude-001.jsonl](../.agent-mesh-runtime/logs/service-claude-001.jsonl)

## What Is Philosophically Stable Now

These concepts now feel stable and should probably remain:

- `service` is the logical unit of responsibility
- `process` is the concrete runtime instance
- `message` is the core IPC unit
- `port` is the local delivery endpoint
- `router` is the node-local switch, not a global brain
- `node` is not part of the minimal microkernel core, but is the first distributed extension layer

This means the local microkernel-style core is:

- `service`
- `process`
- `message`
- `port`

and `node_id` sits one level above them for distributed routing.

## What Is Not Implemented Yet

### 1. Real Remote Transport

`node_id` exists in messages, but the router does not yet send messages to other nodes.

Not yet implemented:

- Unix domain socket transport
- TCP transport
- remote router-to-router forwarding
- node registry / peer table

Current state:

- remote messages can only be detected and deferred

### 2. Dynamic Service Spawn

Services are still static.

Current state:

- services are declared in manifest at startup
- router does not yet create new services at runtime

Not yet implemented:

- `service.spawn` message
- runtime port allocation for new services
- dynamic route insertion
- service registration lifecycle

### 3. Process Lifecycle Management

We now have `process_id`, but lifecycle management is still very thin.

Current state:

- process start is logged
- process stop is logged
- `service.done` is handled

Missing:

- failed state
- restart policy
- timeout policy beyond top-level process timeout
- orphan cleanup
- explicit process registry

### 4. Capability / Policy Model

Current policy is still minimal.

Implemented:

- static route allow/deny

Missing:

- account-aware policy
- per-service capability rules
- object read/write policy
- spawn permissions
- remote trust policy

### 5. Memory / Object Lifecycle

Current object storage works, but it has no lifecycle management.

Missing:

- object GC
- retention policy
- reference tracking
- compaction/snapshot policy

### 6. Additional Service Backends

Currently integrated:

- Codex
- Claude

Not yet integrated:

- Gemini
- OpenClaw
- custom local services

### 7. Observation Tools

Right now the system is observed mainly by logs.

Possible but not implemented:

- `tmux` observer launcher
- live router dashboard
- service/process inspector CLI

## Recommended Next Steps

These are the most important next implementations.

### Priority 1: Real Node-To-Node Wire

Add the first remote transport implementation.

Recommended order:

1. Unix domain socket transport for local structured transport
2. TCP transport between nodes
3. router forwarding based on `to_node`

Why first:

- `node_id` becomes meaningful only when real remote delivery exists

### Priority 2: Process Registry And Lifecycle

Add a lightweight process table.

Minimum additions:

- `process.started`
- `process.failed`
- `process.killed`
- `process.restarted`
- per-service active process map

Why:

- now that `service` and `process` are separated, lifecycle needs to become explicit

### Priority 3: Dynamic Service Spawn

Add runtime creation of services.

Minimum additions:

- `service.spawn` message
- port creation
- adapter process spawn
- service registration update

Why:

- this is a major step from static orchestration to true service composition

### Priority 4: Replace FIFO With Better Transport

FIFO was good for the first prototype, but it is probably not the long-term answer.

Recommended direction:

- move local transport to Unix domain sockets
- keep message format unchanged

Why:

- cleaner multi-writer behavior
- easier structured request/response patterns
- closer to later distributed transport

### Priority 5: Policy And Memory Cleanup

After routing and lifecycle improve:

- add basic capability rules
- add object GC
- add retention policy

## Short Summary

The runtime has already crossed the line from:

- ad hoc CLI orchestration

to:

- a real local microkernel-style service runtime

What is already real:

- service identity
- process identity
- structured messages
- explicit ports
- router-based local switching
- real Codex/Claude service execution
- message body indirection
- first distributed coordinate via `node_id`

What remains is mainly:

- real distributed wire
- lifecycle depth
- dynamic service creation
- policy and memory hardening
