# Skype-like P2P Notes for AIze Federation

## Summary

This note records the current understanding of Skype's historical P2P design and
what AIze should add first if it wants to grow from simple HTTP federation into
a more Skype-like node-to-node system.

## What Skype Did Over Time

### Early Skype

Early Skype was strongly P2P-oriented.

- Peer discovery and overlay routing relied on other Skype nodes.
- Publicly reachable nodes could act as supernodes.
- Clients preferred direct peer-to-peer paths for media and data when possible.
- NAT traversal used UDP hole punching style techniques when possible.
- If direct connectivity failed, Skype fell back to relays.

In practice, the design was not "pure direct P2P only". It was:

- overlay discovery
- direct path first
- relay fallback
- some peers helping other peers

### Later Skype

After Microsoft acquired Skype, the architecture moved away from the earlier
community-supernode model.

- Supernode responsibility shifted toward Microsoft-managed infrastructure.
- The system became more centrally operated.
- Over time it moved further toward cloud-backed services rather than the older
  highly distributed overlay.

So the historical trajectory was:

1. Heavily P2P with peer-assisted overlay and relays.
2. Managed supernodes and more centrally controlled routing.
3. Much more centralized service infrastructure.

## What AIze Has Right Now

Current AIze federation is much simpler.

- Each node stores peer `base_url`.
- Remote delivery is HTTP POST to `/federation/message`.
- Connection establishment is a reciprocal `/federation/connect`.
- There is no NAT traversal.
- There is no long-lived peer channel.
- There is no relay service.
- There is no automatic endpoint refresh or re-discovery.

This means current AIze federation is "reachable HTTP endpoint federation", not
"true NAT-tolerant P2P".

## What AIze Should Add First

If AIze wants to move toward a Skype-like direction, the first additions should
be small and layered. The system should not jump directly into hole punching.

### 1. Peer Identity and Trust

Add stable peer identity before transport complexity.

- `peer_id`
- node public key
- signature on federation handshake
- trust state (`known`, `trusted`, `blocked`)

Reason:

Without stable peer identity, later relay or NAT traversal work has no strong
security boundary.

### 2. Reachability Model in Peer Registry

Extend the peer registry so it can represent more than one static URL.

- public endpoint candidates
- local endpoint candidates
- last-seen endpoint
- transport kind (`http`, `ws`, future `udp`)
- reachability status
- heartbeat timestamps

Reason:

Current `base_url` is too weak. Skype-like behavior needs endpoint candidates
and freshness tracking.

### 3. Long-Lived Peer Link Service

Add a dedicated peer link layer rather than doing everything from HTTPBridge.

Suggested service split:

- `service-peer-registry`
- `service-peer-link`
- `service-peer-relay` later if needed

Reason:

Current federation is just request/response HTTP delivery. To survive endpoint
change, support heartbeats, and enable relay behavior, AIze needs a persistent
peer transport layer.

### 4. Heartbeat and Re-Register

Add periodic liveness and endpoint refresh.

- heartbeat ping
- endpoint refresh
- peer status transitions
- stale peer expiry

Reason:

Even before NAT traversal, AIze needs a way to detect when a saved peer address
has gone stale.

### 5. Message IDs, Ack, Retry

Strengthen delivery semantics before adding harder networking.

- `message_id`
- `ack_id`
- retry queue
- dead-letter or failed-delivery state

Reason:

Current remote delivery is thin. A Skype-like network needs replay-safe routing
and recoverable failure handling.

### 6. Relay Service Before Hole Punching

If NAT traversal becomes important, relay should come before full peer hole
punching.

- relay registration
- relay-approved forwarding
- owner/capability checks for relay use

Reason:

Relay is operationally simpler than full NAT punching and fits AIze's current
microservice direction better.

### 7. Node-Aware Router Contract

Formalize remote routing in the router contract.

- `to_node`
- `from_node`
- peer auth context
- remote delivery result
- remote ack path

Reason:

The router is already the correct abstraction boundary. It should stay node-aware
without pushing remote routing logic up into the UI layer.

## Recommended Implementation Order

The next practical order for AIze is:

1. Peer identity and signed handshake.
2. Rich peer registry with endpoint candidates and heartbeat state.
3. Message ack/retry semantics.
4. Dedicated peer-link service with long-lived transport.
5. Relay service.
6. NAT traversal experiments after relay works.

## Why Not Start With Full NAT Traversal

Starting with full NAT traversal now would be premature.

- The trust model is still too thin.
- Delivery semantics are still too thin.
- Peer state is still just one URL.
- Router-level remote contract is still minimal.

So the correct first move is not "implement Skype". It is:

- make peers identifiable
- make peers refreshable
- make delivery reliable
- then introduce relay
- only then explore direct NAT traversal

## References

- Salman Baset and Henning Schulzrinne, "An Analysis of the Skype Peer-to-Peer Internet Telephony Protocol"  
  https://www1.cs.columbia.edu/~salman/publications/skype1_4.pdf
- Saikat Guha, Neil Daswani, and Ravi Jain, "An Experimental Study of the Skype Peer-to-Peer VoIP System"  
  https://www.microsoft.com/en-us/research/wp-content/uploads/2006/02/iptps06-skype.pdf
- Skype overview and architectural history  
  https://en.wikipedia.org/wiki/Skype
