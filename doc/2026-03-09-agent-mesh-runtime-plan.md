# Agent Mesh Runtime Plan

## Goal

Build a small local-first runtime for CLI services that can later grow into a distributed system.

The first target is not a full platform. The first target is a minimal message-passing runtime that can:

- run multiple CLI services on one Linux machine
- connect them through structured messages instead of `tmux send-keys`
- keep routing and permissions explicit
- treat `tmux` as a debug/view layer, not as the transport itself
- evolve later from local IPC to network transport

## Explicit Non-Goal: Do Not Build A New OS Now

This plan does not propose replacing Linux in the near term.

For now, Linux remains the host OS and should continue to provide:

- process management
- filesystems
- pipes / FIFOs / Unix sockets
- user and group permissions
- network stack
- service management
- resource control

The system built here should live in user space on Linux.

Long-term, the architecture may evolve toward a more OS-like distributed substrate, but that is not the implementation target for the current phase.

## Why Change Direction

The current `tmux`-based experiments proved that:

- `Codex`, `Claude`, and `Gemini CLI` can all emit machine-readable JSON streams
- multiple services can be orchestrated and exchange messages
- pane IDs and direct `send-keys` are enough for experiments

But they also exposed limits:

- `send-keys` is tied to TTY behavior and UI state
- pane IDs are local debug identifiers, not real distributed addresses
- transport, routing, and policy are currently mixed together
- the design does not yet scale cleanly to multi-node communication

## Design Direction

Use a small message-passing model closer to a microkernel or distributed runtime.

Core idea:

- services are processes
- communication happens through message envelopes
- local transport is FIFO or Unix socket
- remote transport can later be TCP/WebSocket/etc.
- `tmux` is optional and only used to observe processes

## OpenFang-Inspired Essence To Keep

OpenFang is a useful reference mainly because it separates the system into strong layers:

- `kernel`
- `runtime`
- `wire`
- `cli`
- `memory`

That separation is worth preserving.

For this project, the useful parts of the OpenFang approach are:

- a kernel-like layer for routing, policy, scheduling, and lifecycle
- a runtime layer for service execution and tool use
- a wire layer that makes local and remote messaging conceptually compatible
- a CLI layer that is only one client/entrypoint, not the core system
- a memory layer that stores persistent state separately from transient execution

What should not be copied right now:

- the large channel surface area
- the desktop/dashboard-heavy product shape
- the full security stack
- the large number of bundled service packages
- the assumption that the system must be a monolithic “Agent OS” binary from day one

The aim is to preserve the layering, not the size.

## Core Concepts

### Principal

A logical entity.

Examples:

- a human user
- a local service
- a remote service
- a system service

### Account

An execution identity for a principal.

An account carries policy and runtime constraints.

Examples:

- `user.alice`
- `service.codex_worker`
- `service.claude_worker`

### Process

A running executor bound to an account.

Examples:

- `codex exec --json`
- `claude -p --output-format stream-json`
- `gemini --output-format stream-json`
- `openclaw` wrapper process

### Object

A file-like or resource-like thing in the system.

Examples:

- memory file
- profile document
- append-only conversation log
- Unix socket
- FIFO port

### Stream

A time-ordered I/O object used for messages or events.

Examples:

- service receive port
- service stdout JSONL event stream
- shared conversation event log

### Policy

Rules controlling who may:

- read an object
- write an object
- send to a stream
- invoke a process

### Host Boundary

The actual machine/process/container/pane constraints that exist regardless of logical permissions.

Examples:

- Linux user permissions
- filesystem reachability
- working directory scope
- available binaries
- sandboxing

## Non-Goals For The First Version

Do not build these yet:

- global cluster scheduling
- replicated storage
- full multi-user authentication
- GUI
- automatic service discovery
- generic plugin system

These can come later.

## First Version Scope

Build a single-node runtime with structured local IPC.

### Required

- static service registry
- static route policy
- local port transport using FIFO or Unix socket
- JSONL-based service adapters
- run IDs and service IDs in every message
- persistent logs for runs and events
- a visible `kernel/runtime/wire/memory/cli` split in the code layout, even if minimal

### Optional

- `tmux` launcher for observing services
- simple CLI to send seed messages
- one shared conversation log object

## Proposed Architecture

### 1. Router

A small local router process that:

- accepts outbound messages from services
- validates route policy
- forwards to local ports
- logs the message

This is not intended to be a “central brain”.
It is a transport/policy component for a single node.

Later, nodes can route to each other.

This is the first minimal `kernel` layer.

### 2. Service Adapters

Each external CLI service gets a thin adapter process.

Responsibilities:

- read inbound messages from receive port
- invoke the underlying CLI
- normalize output to JSONL events
- emit outbound messages back to the router

Adapters needed:

- `codex-adapter`
- `claude-adapter`
- `gemini-adapter`
- `openclaw-adapter`

This is the first minimal `runtime` layer.

### 3. Local Transport

Use one of:

- FIFO per service
- Unix domain socket per service

Preferred starting point:

- FIFO for simplicity

Possible layout:

- `runtime/ports/service-codex-001.rx`
- `runtime/ports/service-claude-001.rx`
- `runtime/ports/service-gemini-001.rx`

This is the first local form of the `wire` layer.

### 4. Event Log

Use append-only JSONL logs.

Possible layout:

- `runtime/logs/router.jsonl`
- `runtime/logs/service-codex-001.jsonl`
- `runtime/logs/service-claude-001.jsonl`

This is the first minimal `memory` layer.

## Message

First version message:

```json
{
  "message_id": "msg-001",
  "run_id": "run-001",
  "from": "service-codex-001",
  "to": "service-claude-001",
  "type": "prompt",
  "ts": "2026-03-09T00:00:00Z",
  "payload": {
    "text": "Please search the web for one recent software engineering fact."
  }
}
```

Later fields:

- `node_id`
- `account_id`
- `trace_id`
- `capabilities`
- `reply_to`

### Node And Microkernel Scope

`node` is not part of the minimal microkernel core.

The local core concepts stay:

- `service`
- `process`
- `message`
- `port`

`node` is the first distributed extension layer added above that core.

Practical meaning:

- `service_id`
  what logical service is being addressed
- `process_id`
  which concrete running instance is doing the work
- `node_id`
  which runtime island or host-local message world the service/process belongs to

So `node_id` is closer to a distributed routing coordinate than to a local kernel primitive.

## Service Event Shape

Normalize every adapter to emit events like:

```json
{
  "type": "service.event",
  "service_id": "service-codex-001",
  "run_id": "run-001",
  "stream": "assistant",
  "seq": 3,
  "data": {
    "text": "Here is the result..."
  }
}
```

Useful stream values:

- `lifecycle`
- `assistant`
- `tool`
- `error`
- `result`

## Why OpenClaw Matters

`OpenClaw` is relevant because it already has an internal event bus:

- `emitAgentEvent(...)`
- `onAgentEvent(...)`

That bus is currently in-process, but it is exactly the right raw material for an adapter.

Plan:

- subscribe to OpenClaw internal events
- write them to stdout as JSONL
- wrap OpenClaw like the other CLI services

That makes OpenClaw another process in the mesh rather than a special control plane.

## Layering Summary

The intended code split should stay small but explicit:

- `kernel`
  routing, policy checks, lifecycle tracking
- `runtime`
  service adapters and execution loops
- `wire`
  FIFO now, network transport later
- `memory`
  logs, sessions, object files
- `cli`
  launch, inspect, seed, debug

Even in a very small prototype, keeping these boundaries visible should prevent the design from collapsing back into an ad hoc `tmux` script.

## Role Of tmux

`tmux` should not be the transport.

`tmux` should only be:

- a process viewer
- a local debugging console
- an easy way to inspect stdin/stdout behavior

This is an important boundary:

- real communication goes through ports
- `tmux` only helps humans observe the node

## Main Open Questions

### 1. Router vs fully peer-to-peer

Question:

- should local node routing start with a small router process
- or should each service write directly to the target port

Current answer:

- start with a router for clarity and logging
- keep the message format compatible with later peer-to-peer routing

### 2. FIFO vs Unix socket

Question:

- is FIFO enough for now
- or should the first version use Unix sockets

Current answer:

- start with FIFO
- move to Unix sockets when bidirectional structured I/O becomes limiting

### 3. State model

Question:

- should conversation state live only in logs
- or should there be a separate object store

Current answer:

- start with append-only logs plus small state files
- avoid building a full database first

### 4. Permissions model

Question:

- how much policy is required in v1

Current answer:

- route allow/deny
- per-service port ownership
- explicit account names in config

Deeper capability systems can come later.

### 5. Session model

Question:

- should each run be stateless
- or should adapters maintain resumable sessions

Current answer:

- allow adapters to keep provider-specific session state
- but keep the mesh message format session-agnostic at first

## Minimal Implementation Plan

### Phase 1: Local Message Runtime

- define manifest format for services and routes
- create port directory structure
- implement router process
- implement message logger

### Phase 2: Agent Adapters

- codex adapter
- claude adapter
- gemini adapter
- openclaw adapter

Each adapter must:

- read one message
- run one turn
- emit JSONL events
- emit one outbound message if needed

### Phase 3: Observation

- optional `tmux` launcher
- tail logs in panes
- inspect port activity

### Phase 4: First Distributed Boundary

- add `node_id`
- add TCP/WebSocket transport stub
- allow forwarding envelopes to another node

## Suggested Directory Layout

```text
runtime/
  manifest.json
  ports/
    service-codex-001.rx
    service-claude-001.rx
  logs/
    router.jsonl
    service-codex-001.jsonl
    service-claude-001.jsonl
  state/
    runs/
    sessions/
    objects/
```

## Success Criteria

The first version is successful if:

- two local services can exchange structured messages without `tmux send-keys`
- all communication is logged as JSONL
- route policy is explicit
- `tmux` is optional
- one adapter can later be replaced by a networked endpoint without redesigning the protocol

## Immediate Next Step

Build the smallest possible local runtime:

- one router
- two FIFO ports
- one `Codex` adapter
- one `Claude` adapter
- one seed message
- JSONL logs everywhere

That is enough to replace the current `send-keys` experiment with a proper local message-passing base.
