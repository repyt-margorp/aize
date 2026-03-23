# Distributed OSS Comparison

## Purpose

This note compares the current service-microkernel direction against a small set of relevant open-source projects.

The goal is not to copy any single project wholesale.

The goal is to identify:

- concepts that are genuinely useful
- concepts that fit the current kernel/service/message philosophy
- concepts that should be delayed or rejected for now

## Comparison Set

The comparison focuses on these projects:

- OpenFang
- OpenClaw
- AutoGen Core
- AGNTCY

These four matter for different reasons:

- OpenFang is closest to an explicit Agent/Service OS structure
- OpenClaw is strongest on session/gateway/channel reality
- AutoGen Core is strongest on distributed actor-style messaging
- AGNTCY is strongest on interop, identity, and network-layer protocols

## Evaluation Lens

The current project is trying to become:

- a Linux user-space microkernel-style runtime
- service-oriented rather than AI-tool-first
- local-first in implementation
- distributed-ready in message structure

So the useful questions are:

1. Does the project separate logical service identity from runtime execution?
2. Does it use message-passing as a first-class primitive?
3. Does it distinguish local runtime concerns from distributed routing concerns?
4. Does it define identity and discovery in a way that can survive multi-node scaling?
5. Does it stay conceptually compatible with the current kernel/service model?

## Project-by-Project Reading

### 1. OpenFang

What stands out:

- explicit layered architecture
- `kernel`, `runtime`, `memory`, `wire`, `cli`
- wire is already treated as a separate concern
- memory is not just chat history; it is part of system structure

Most useful concepts to adopt:

- explicit layer boundaries
- wire as a first-class subsystem
- lifecycle and scheduling as kernel concerns
- signed manifests / identity material
- clear separation between runtime execution and transport

Why it fits the current direction:

- it is the closest match to a service-microkernel framing
- it already treats orchestration and transport as different layers

What to be careful about:

- OpenFang is already broad and productized
- it bundles much more than is needed right now
- if copied too directly, it would bloat the current project

What is probably worth borrowing now:

- layer vocabulary
- process lifecycle thinking
- wire abstraction
- manifest/signing direction

What is probably worth delaying:

- large channel surface
- REST/dashboard sprawl
- too many bundled service packages

### 2. OpenClaw

What stands out:

- local-first gateway model
- multi-agent routing with isolated sessions and workspaces
- strong session handling
- real delivery surfaces and practical channel bindings

Most useful concepts to adopt:

- isolated workspaces/state per logical service
- session tooling
- channel/account/binding separation
- message delivery and retry awareness
- strong distinction between persona/workspace/state

Why it fits partially:

- it is very good at real-world routing and session isolation
- it is much less clean as a microkernel than OpenFang

What to be careful about:

- OpenClaw is gateway-centric
- `channel` is a product-facing abstraction, not a microkernel primitive
- its control plane is more "assistant gateway" than "service kernel"

What is probably worth borrowing now:

- session store ideas
- isolated service workspace/state layout
- routing bindings as a policy layer
- service-to-service session tools

What is probably worth delaying:

- channel-heavy surface area
- app/mobile/device integration
- gateway-specific product concepts as core runtime concepts

### 3. AutoGen Core

What stands out:

- explicit event-driven messaging
- actor-model framing
- local-to-distributed continuity
- cross-language agent/service runtime direction

Most useful concepts to adopt:

- asynchronous message passing
- actor-ish service identity
- distributed runtime continuity
- observable event streams
- typed messages for runtime communication

Why it fits:

- it validates the "local first, distributed later" path
- it shows that event-driven multi-service systems scale better than ad hoc orchestration

What to be careful about:

- AutoGen is a framework first, not a service microkernel
- it may lead toward application orchestration abstractions rather than OS-like resource abstractions

What is probably worth borrowing now:

- event-driven service interaction
- typed message discipline
- service boundary clarity
- cross-runtime / cross-language mindset

What is probably worth delaying:

- framework-heavy workflow abstractions
- cloud/runtime dependencies that assume a bigger deployment story too early

### 4. AGNTCY

What stands out:

- identity
- discovery
- schema standardization
- network-level inter-agent messaging
- directory model

Most useful concepts to adopt:

- separate service description schema from runtime state
- directory/discovery layer
- network messaging protocol as a different layer from local IPC
- identity/authentication material for services and tools
- observability/evaluation as infrastructure, not afterthought

Why it fits:

- AGNTCY is not a microkernel, but it is very relevant for distributed service interop
- it helps answer what happens after the first node

What to be careful about:

- AGNTCY is ecosystem/internet-of-agents oriented
- it can pull the design toward standards and registries before the local runtime is mature

What is probably worth borrowing now:

- service description schema direction
- identity/directory separation
- "local transport" vs "network transport" distinction

What is probably worth delaying:

- hosted directory complexity
- full standardization work before the local kernel is stable

## Concept Extraction

The concepts that look both necessary and compatible with the current direction are these.

### A. Keep As Core Concepts

- `service`
- `process`
- `message`
- `port`
- `router`
- `object`

These are still the right local microkernel core.

### B. Add Soon

- `process lifecycle`
- `service registry`
- `service spawn`
- `node`
- `wire transport abstraction`
- `object GC / retention`

Why:

- these extend the current core without breaking the microkernel model

### C. Add Later But Definitely Important

- `identity material` for services
- `signed manifests`
- `remote directory / discovery`
- `retry / delivery policy`
- `capability-aware policy`
- `remote transport authentication`

Why:

- these matter once multi-node communication becomes real

### D. Avoid Treating As Core

- channels
- chat surfaces
- mobile nodes
- UI/dashboard concepts
- assistant-persona product abstractions

Why:

- they are valid product/runtime adapters
- they are not microkernel primitives

## What Seems Most Validated By The Comparison

After comparing the projects, the current direction looks strongest when stated like this:

1. Keep the local kernel vocabulary small:
   - service
   - process
   - message
   - port
   - object

2. Treat `node` as the first distributed extension layer, not as part of the minimal kernel core.

3. Treat `wire` as its own subsystem:
   - local FIFO/Unix socket now
   - remote TCP/other transport later

4. Treat `service registry` and `process lifecycle` as the next major missing kernel functions.

5. Treat `identity`, `directory`, and `interop schema` as distributed infrastructure layers above the local microkernel core.

## Recommended Next Implementations After This Comparison

### Priority 1

Process lifecycle table:

- active process registry
- process state transitions
- restart/failure tracking

This is strongly supported by OpenFang and also required by the current service/process split.

### Priority 2

Dynamic service spawn:

- `service.spawn`
- port allocation
- service registration
- process creation

This is the cleanest next microkernel step.

### Priority 3

Wire abstraction beyond FIFO:

- local Unix domain sockets
- remote node forwarding
- explicit remote delivery handling

This is where AutoGen and AGNTCY become more directly relevant.

### Priority 4

Service identity material:

- manifest signing
- service credentials
- trust boundaries

This is where OpenFang and AGNTCY are most useful references.

## Short Conclusion

The comparison suggests:

- OpenFang is the best architectural reference
- OpenClaw is the best operational/session reference
- AutoGen is the best distributed messaging reference
- AGNTCY is the best interop/identity reference

The current project should therefore continue with:

- OpenFang-like layer discipline
- OpenClaw-like isolation and session realism
- AutoGen-like event-driven distributed service thinking
- AGNTCY-like future identity/discovery separation

But the local microkernel core should still stay small:

- service
- process
- message
- port
- object
