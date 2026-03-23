# Core Message Envelope for AIze

## Decision

AIze message handling should converge on a small core envelope, not on an
inherited OS-analogy identity or naming frame.

The core routing fields are:

- `from`
- `to`
- `type`

The normal content field is:

- `payload`

Large bodies may continue to use `payload_ref` during migration, but that is an
optimization detail rather than the philosophical center.

## Rationale

The current message format has accumulated too many concerns in one structure:

- user conversation routing
- service control
- provider event forwarding
- restart resume metadata
- authentication context
- run tracking

That makes the envelope harder to reason about and harder to evolve beyond
LLM-to-LLM traffic.

The new direction is to keep the envelope simple:

- who sent it
- who receives it
- what kind of message it is
- the content or arguments

Everything else should be treated as optional metadata or reconstructed by the
receiving layer when needed.

## Target Shape

Canonical message:

```json
{
  "from": "service-http-001",
  "to": "service-codex-001",
  "type": "prompt",
  "payload": {
    "text": "hello"
  }
}
```

## Migration Direction

### Keep as core routing fields

- `from`
- `to`
- `type`

### Keep as the default body field

- `payload`

### Keep as compatibility / optimization metadata

- `conversation`
- `auth`
- `run_id`
- `process_id`
- `from_node`
- `to_node`
- `payload_ref`

These may still exist during migration, but they are no longer part of the
core required message philosophy.

## Interpretation Rule

Each service should interpret `payload` according to `type`.

Examples:

- `type=prompt`: payload contains text or prompt body
- `type=event`: payload contains event data
- `type=service.start`: payload contains service id
- `type=service.compact`: payload contains compact request details

## Why This Fits AIze

This aligns with the intended direction:

- simpler mailbox contract
- less protocol noise in every message
- easier support for non-LLM services
- clearer kernel/service boundary without importing an unnecessary external framing

## Next Implementation Step

Refactor router, adapters, and HTTPBridge so they depend primarily on:

- `from`
- `to`
- `type`
- `payload`

and treat the rest as compatibility metadata while migrating.
