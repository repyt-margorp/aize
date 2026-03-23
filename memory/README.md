# Memory Layer

The first version keeps memory deliberately small.

- append-only JSONL logs live under `.agent-mesh-runtime/logs/`
- manifest and small state files live under `.agent-mesh-runtime/`
- session and object storage can be added later under `.agent-mesh-runtime/state/`

This directory exists to keep the `kernel/runtime/wire/memory/cli` split explicit from the first prototype.

Current runtime layout under `.agent-mesh-runtime/`:
- `ports/` for FIFO-based local port endpoints
- `logs/` for append-only JSONL event logs
- `objects/` for large referenced payload bodies
- `state/` for runtime state files
