# Aize Current Architecture Status

## Purpose

This note describes the current implemented architecture of Aize as it exists in the codebase on 2026-03-23.
It is not a proposal document. It is a snapshot of what is actually wired together now, which parts are stable,
and which design directions are already visible in the implementation.

The repository currently implements a local multi-service agent mesh with these major properties:

- A local microkernel-style message router
- Dynamically spawned service workers for Codex, Claude, HTTP, and service management
- Persistent session state stored outside the runtime directory
- A browser-facing HTTPS bridge (`HttpBridge`)
- Goal-oriented session control and GoalManager review/compact flows
- Local and remote peer/federation hooks

The current code is substantially beyond the earlier design memos. The important shift is that the system is now
an actual runnable service mesh with persistent user/session state, not just a planned service decomposition.

## Top-Level Shape

The implemented system is split into five layers:

1. `src/cli/`
   Bootstraps a runtime instance, writes a manifest, and launches the mesh.
2. `src/kernel/`
   Owns service registry, process lifecycle records, service authorization, IPC transport, and routing.
3. `src/services/`
   Defines service descriptors (`service.json`) and expands them into concrete service specs.
4. `src/runtime/`
   Implements service adapters, HTTPBridge, provider execution, GoalManager logic, compaction, session views, and websocket peer behavior.
5. `src/runtime/persistent_state_pkg/`
   Owns persistent auth/session/history/conversation state and the session-scoped file model.

This is not a purely in-memory actor system. It is a hybrid:

- Runtime orchestration state lives under `.agent-mesh-runtime/`
- Durable user/session state lives under `.aize-state/`
- Source-tracked service definitions live under `src/services/`

## Boot Sequence

The canonical entrypoint for the current HTTP mesh is `src/cli/run_codex_http_mesh.py`.

At startup it does the following:

1. Resolves `AIZE_ROOT` and `AIZE_RUNTIME_ROOT`
2. Decides HTTP bind settings
   For the primary runtime root, it forces `0.0.0.0:4123` with TLS enabled by default
3. Captures restart-resume state for previously known services when possible
4. Builds a bootstrap runtime manifest with:
   - `node_id`
   - `run_id`
   - peer metadata
   - only the Service Manager (`service-svcmgr-001`) as a boot-time service
   - `svcmgr` restart/restore metadata for descriptor-managed and dynamically restored services
5. Rebuilds `.agent-mesh-runtime/`
   It preserves TLS material and outbound WS peer client config across restart
6. Starts the kernel router
7. Starts the bootstrap service adapters
8. Lets `service-svcmgr-001` discover descriptors and spawn `HttpBridge`, Codex, Claude, and restored dynamic services

The runtime manifest is generated at boot and lives under `.agent-mesh-runtime/manifest.json`.
It is a bootstrap manifest, not the full long-term service inventory, and is not treated as a source-of-truth file checked into the repository.

## Service Model

Services are declared by descriptor and expanded into concrete service records at runtime.

Current built-in service kinds:

- `codex`
- `claude`
- `http`
- `svcmgr`

### Descriptor Expansion

`src/services/svcmgr/loader.py` scans `src/services/*/service.json`, skips disabled entries, resolves
`config_env`, and expands pool-based services into fixed concrete IDs.

Current built-in pools:

- Codex pool: `service-codex-001` through `service-codex-005` by default
- Claude pool: `service-claude-001` through `service-claude-005` by default

Current fixed services:

- `service-http-001`
- `service-svcmgr-001`

This means the system already behaves more like a small local cluster than a single-agent app.

### Ownership and Authorization

Every service record carries:

- `owner_principal`
- `owner_roles`
- `owner_capabilities`
- `allowed_peers`

This is important because service spawn/control is not treated as implicitly trusted once messages enter the kernel.
The kernel checks capabilities and route permissions rather than assuming that all local traffic is allowed.

## Kernel Architecture

The kernel layer currently consists of:

- `auth.py`
- `ipc.py`
- `lifecycle.py`
- `peers.py`
- `registry.py`
- `router.py`
- `spawn.py`

### Router

`src/kernel/router.py` is the central broker.

Responsibilities:

- Load the runtime manifest
- Initialize registry and lifecycle state
- Open the UNIX router socket
- Accept service connections
- Register connections after IPC handshake
- Route local messages over connected sockets
- Forward remote messages to peers
- Handle kernel-recipient messages for spawn/control
- Write structured router logs

The router has explicit reserved recipients:

- `kernel.spawn`
- `kernel.control`

Messages addressed to those IDs are intercepted and translated into spawn/control actions instead of being forwarded like ordinary service traffic.

### IPC Transport

The current transport is UNIX domain sockets, not the earlier FIFO design.

`src/kernel/ipc.py` defines:

- `router.sock` as the single router socket
- handshake type `ipc.connect`
- thread-safe `RouterConnection`

Protocol shape:

1. Service connects to `router.sock`
2. Service sends `{"type":"ipc.connect","service_id":"..."}`
3. Router binds the socket to that service ID
4. Messages are exchanged as newline-delimited JSON over the same socket

This is an important design stabilization point:

- The codebase has already moved away from the earlier per-service FIFO idea
- Service records still retain a historical `ports` field, but the active transport is the router socket

### Registry

`src/kernel/registry.py` stores runtime service records in `.agent-mesh-runtime/state/services.json`.

It tracks:

- service identity and kind
- display metadata
- config
- process linkage
- allowed peers
- ownership metadata
- status timestamps

The registry is used as the runtime service authority.
It is not just informational; spawn/control and UI views depend on it.

### Spawn Manager

`src/kernel/spawn.py` implements dynamic service spawn and service control.

Current behavior:

- Validates `spawn_service`, `control_service`, or `read_service_status` capabilities
- Registers dynamically spawned services into registry state
- Adds reverse routes for service peers
- Launches `runtime.cli_service_adapter` per service
- Tracks and updates process state
- Supports `start`, `stop`, `restart`, `reload`, and `status`

This means the runtime is already able to grow beyond the static core pool.

## Runtime State Split

The codebase now clearly separates two kinds of state.

### `.agent-mesh-runtime/`

Used for runtime-local process state:

- manifest
- ports
- logs
- process/lifecycle state
- service registry state
- TLS materials

This directory is treated as disposable/rebuildable across restart, except for explicitly preserved runtime artifacts like TLS certs.

### `.aize-state/`

Used for durable user/session state:

- users
- auth sessions
- per-user sessions
- session metadata
- timeline history
- pending inputs
- service runtime bindings
- goal manager state and review logs
- per-agent files and ACLs
- session DAG parent/child relationships

This split is implemented in `src/runtime/persistent_state_pkg/_core.py`:

- Canonical repo runtime roots named `.agent-mesh-runtime*` store durable state at the sibling path `runtime_root.parent / ".aize-state"`
- Ephemeral/test runtime roots store durable state under `runtime_root / ".aize-state"` to avoid collisions under shared scratch parents

That is a strong design signal:

- The durable conversation model is intentionally outside the disposable mesh runtime directory

## Authentication and User Model

The auth model is implemented in `src/kernel/auth.py` and the persistent auth helpers in `src/runtime/persistent_state_pkg/auth.py`.

Current roles:

- `root`
- `superuser`
- `user`

Current effective capability model:

- `root` and `superuser` both imply:
  - `spawn_service`
  - `manage_users`
  - `control_service`
  - `read_service_status`
  - `superuser`
- `user` has no elevated kernel/service capabilities by default

### Root Bootstrap

The first account is created through `/bootstrap` in HTTPBridge.

Current behavior:

- If no users exist, root bootstrap is allowed
- The created account is named `root`
- It receives both `root` and `superuser` roles
- Password hashes use PBKDF2-HMAC-SHA256 with 200,000 rounds

### Current Security Posture

Passwords and auth sessions are not stored in source-controlled files.

They live under runtime state and durable state only:

- `.aize-state/`
- `.agent-mesh-runtime/`

This is now consistent with the public-repo requirement:

- boot-critical code is in Git
- runtime/user secrets are not

## Persistent Conversation Model

The conversation/session system is no longer a thin history list.
It is a structured per-session state machine stored on disk.

### Per-Session Storage

Each session has its own directory under `.aize-state/sessions/<user>/<session_id>/`.

Current stored concerns include:

- `session.json`
- `timeline.jsonl`
- pending input queues
- service state files
- GoalManager state
- goal directories and goal attachments
- session DAG structure
- agent-scoped file areas

### Session Features Already Implemented

The persistent session model currently supports:

- multiple sessions per user
- selection of active session
- session rename
- service leasing/binding
- per-session provider preference
- agent priority ordering
- session priority score
- auto-compact threshold
- goal text / goal flags / active goal ID
- auto-resume schedule state
- peer joinability
- selected agents
- welcomed agent contacts
- parent/child session DAG relationships

This is significantly more advanced than a simple chat history store.

## Agent File Model

One of the more important current design choices is the per-agent file system living inside a session.

Implemented in `src/runtime/persistent_state_pkg/conversation.py`.

Current concepts:

- per-session agent file area
- inbox / outbox split
- per-agent directory isolation
- ACL file per agent directory

Current public operations:

- write agent file
- read agent file
- list agent files
- delete agent file
- get/set directory ACL
- check agent file ACL

The design implication is that the system now supports agent-scoped artifact exchange as a first-class session primitive, not only plain text turns.

## HTTPBridge

`service-http-001` exposes the browser-facing system.
The implementation spans `src/runtime/cli_service_adapter.py`, `src/runtime/http_handler.py`, and `src/runtime/html_renderer.py`.

### Current Role

HTTPBridge is not a thin proxy.
It is a substantial application layer with:

- login/bootstrap flows
- session selection and navigation
- session map
- message posting
- goal editing
- settings editing
- agent controls
- manual compact
- session-scoped file APIs
- goal attachment upload/list/read
- registration of users by superuser
- federation endpoints
- health/events/messages/session APIs

### UI Model

The current UI is a single-page style browser shell rendered by `render_main_page`.

Major UI regions:

- sidebar with session navigation and superuser tools
- talk pane for the active session
- session-map pane for graph/grid overview
- account-registration pane
- popovers for goal/settings/agent controls

The UI has already absorbed many operational controls that were previously only script-level concepts.

### Auth and Session Handling

HTTPBridge uses cookie-based auth sessions via `bridge_session`.

Current auth flow:

- `/bootstrap` creates first root account
- `/login` verifies password and creates auth session
- `/logout` deletes auth session
- `/register` creates users for superuser sessions

### HTTPS

TLS is enabled by default.
The HTTP service adapter wraps the server socket with `ssl.SSLContext`, and self-signed cert generation is supported in `src/tls/gen_self_signed_cert.py`.

## Goal and GoalManager Model

Goal handling is no longer just a free-text note.
It is now a structured part of session execution.

Relevant modules:

- `src/runtime/goal_audit.py`
- `src/runtime/goal_persist.py`
- `src/runtime/compaction.py`
- `src/runtime/session_view.py`

### Current Goal State

Per-session goal state includes:

- `goal_text`
- `goal_id`
- `active_goal_id`
- active/inactive flags
- completed/in-progress state
- audit state
- reset-on-prompt behavior
- autonomous compact enablement
- GoalManager runtime state
- goal review cursor

### GoalManager Responsibilities

The current GoalManager path can:

- review recent session history
- persist goal audit outcomes
- request compaction
- emit follow-up directives
- track review cursors and state
- persist compact events
- coordinate with session context status

The important design evolution is that goal review is implemented as an operational loop with persistence, not a UI-only metadata field.

## Provider Runtime

Provider-specific execution currently exists for:

- Codex
- Claude

Relevant modules:

- `src/runtime/providers/codex.py`
- `src/runtime/providers/claude.py`
- provider execution integration inside `cli_service_adapter.py`

Current provider runtime concerns include:

- running normal turns
- compaction turns
- context window checks
- service response parsing
- audit/continue loops

The pool model plus session leasing means a session can be bound to a provider worker while still being managed through shared runtime infrastructure.

## Compaction and Context Management

Compaction is now a core runtime behavior, not an external maintenance task.

Implemented concepts include:

- per-session auto-compact thresholds
- manual compact
- provider-specific compact flows
- GoalManager-triggered compact
- session context status persistence
- audit-state clearing after successful manual compact

This is one of the clearest places where Aize behaves like an operating environment for long-running agent work rather than a conventional chat wrapper.

## Federation and Peer Support

The codebase includes a real federation/peer direction, not just notes.

Current pieces:

- `kernel.peers`
- `/peer/ping`
- `/federation/connect`
- `/federation/message`
- websocket peer client/handler

This is still less central than the local mesh, but the architecture is already prepared for multiple nodes and remote delivery.

## Service Adapter Role

`src/runtime/cli_service_adapter.py` is currently the largest integration point in the system.

It is doing several jobs at once:

- service process bootstrap
- provider dispatch integration
- HTTP service hosting
- auth/session bridging
- GoalManager orchestration
- pending input draining
- session/service leasing
- restart resume handling
- websocket peer client startup

Architecturally, this file is the current "runtime supervisor and service host" layer.
It is functional, but it is also the main concentration point for future refactoring if the project wants a cleaner split between:

- provider workers
- HTTP gateway
- scheduler/orchestrator
- goal runtime

## Current Design Direction

Reading the implementation as a whole, the current design direction is clear:

- The system is moving toward a local agent operating environment
- Sessions are durable workspaces, not just chats
- Services are managed compute actors with explicit routing and authorization
- HTTPBridge is becoming a serious control surface, not only a demo UI
- GoalManager is becoming a persistent supervisory subsystem
- Artifact/file exchange is first-class
- Federation remains a supported extension point

The dominant architectural theme is:

"persistent multi-session work orchestration over a local service mesh"

not:

"single assistant chat app"

## Practical Constraints Visible in the Current Code

The implementation is already useful, but several constraints are visible:

- `cli_service_adapter.py` remains highly concentrated and carries many responsibilities
- runtime restart/process replacement can still be operationally delicate if multiple local launches overlap
- the registry schema still carries some historical transport assumptions while the active IPC path is socket-based
- UI logic is substantial and string-rendered, so feature work there is fast but can become structurally dense

These are not blockers, but they are the main areas where future refactoring effort will likely pay off.

## Current Stable Invariants

As of the current implementation, these are safe assumptions to design around:

- The runtime is booted from generated service descriptors, not a checked-in manifest
- The kernel routes over a UNIX router socket
- Durable user/session state lives in `.aize-state`
- Runtime process state lives in `.agent-mesh-runtime`
- HTTPBridge is the canonical browser control plane
- Root bootstrap and cookie-based auth are part of the normal product path
- Goal state, compaction state, and session scheduling are persisted
- Service pools for Codex and Claude are first-class
- Public Git history should contain boot-critical code but omit runtime/user secrets

## Summary

The codebase is no longer in an exploratory architecture phase.
It already implements a coherent system with these concrete characteristics:

- local service mesh kernel
- authenticated multi-user HTTP UI
- persistent sessions as workspaces
- pooled provider workers
- structured goal supervision
- compaction-aware long-running operation
- artifact-aware session state
- early federation hooks

The main design challenge now is not "what should Aize be?" but "how should the existing runtime be decomposed and cleaned up as it grows?"
