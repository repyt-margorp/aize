# Memory Layer

The first version keeps memory deliberately small.

- append-only JSONL logs live under `.aize-runtime/logs/`
- manifest and small state files live under `.aize-runtime/`
- session and object storage can be added later under `.aize-runtime/state/`

This directory exists to keep the `kernel/runtime/wire/memory/cli` split explicit from the first prototype.

Current runtime layout under `.aize-runtime/`:
- `ports/` for FIFO-based local port endpoints
- `logs/` for append-only JSONL event logs
- `objects/` for large referenced payload bodies
- `state/` for runtime state files
