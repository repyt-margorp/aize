# Structured Schema And Codex Resume Status

## Scope

This note records the implementation status reached on 2026-03-15 for:

- service-local structured output
- Codex session resume
- three-service Codex conversation completion

This document is intentionally narrow.
It focuses on the concrete changes made in this phase rather than the whole runtime plan.

## What Changed

### 1. Service-Local Structured Schema

The temporary `@@spawn ...` control syntax is no longer the primary mechanism.

Instead, the runtime now supports a service-local structured response schema:

- `service_control_v1`

The schema file is:

- [runtime/schemas/service_control_v1.json](../runtime/schemas/service_control_v1.json)

This schema is intentionally separate from the wire/message schema.

The separation is:

- wire schema
  runtime IPC between services
- service-local response schema
  provider/backend output contract for one service kind

Current `service_control_v1` fields:

- `assistant_text`
- `spawn_requests`

Each spawn request contains:

- `service`
- `allowed_peers`
- `initial_prompt`

The service definition now includes:

- `service_id`
- `kind`
- `display_name`
- `persona`
- `response_schema_id`
- `max_turns`

### 2. Adapter-Level Schema Handling

Structured output is handled only in the adapter layer.

The relevant implementation is:

- [runtime/cli_service_adapter.py](../runtime/cli_service_adapter.py)

The adapter now does the following:

- builds schema-aware prompt instructions
- invokes the backend with schema support when available
- parses provider output into structured JSON
- validates the result against `service_control_v1`
- converts `assistant_text` into a normal outbound runtime message
- converts `spawn_requests` into `service.spawn` kernel messages

This preserves the intended boundary:

- kernel and wire do not depend on provider-specific output shape
- only the adapter knows how to bridge provider output into runtime messages

### 3. Codex Structured Output

Codex now uses:

- `codex exec --json --output-schema <schema>`

for the first turn of a session.

This was verified locally with a minimal schema response.

Observed successful example:

```json
{"assistant_text":"hello","spawn_requests":[]}
```

The adapter can parse this into:

- visible text: `hello`
- no spawn requests

### 4. Claude Structured Output

Claude now uses:

- `claude -p --output-format stream-json --json-schema <schema-json>`

The adapter extracts structured output from:

- `result.structured_output`

rather than relying only on the plain `result` string.

This was also verified locally with the same schema and the same minimal output:

```json
{"assistant_text":"hello","spawn_requests":[]}
```

This means Codex and Claude now share the same service-local schema contract.

### 5. Codex Resume

Codex services now persist session continuity through:

- `codex exec --json ...` on the first turn
- `codex exec resume --json <session_id> <prompt>` on later turns

The adapter stores:

- `codex_session_id`

inside process lifecycle state.

The relevant state file is:

- [.agent-mesh-runtime/state/processes.json](../.agent-mesh-runtime/state/processes.json)

The adapter extracts `thread.started.thread_id` from Codex JSON events and saves it as the process session id.

This was verified locally by running two Codex turns in sequence and observing:

- first response used a new session
- second response reused the same session id

### 6. Important Limitation Discovered

`codex exec resume` does **not** accept:

- `--output-schema`

This was confirmed locally by direct execution.

Observed failure:

```text
error: unexpected argument '--output-schema' found
```

So the current Codex strategy is:

- first turn:
  use CLI-enforced schema with `--output-schema`
- resumed turns:
  keep schema instructions in the prompt
  validate the returned JSON in the adapter

This is a real CLI limitation, not a runtime design choice.

### 7. Three-Service Codex Conversation

The current demo run:

- starts `CodexFox`
- lets `CodexFox` spawn `CodexNorth`
- lets `CodexFox` spawn `CodexSouth`
- continues the three-way conversation until all services stop

The driver remains:

- [cli/run_codex_claude_mesh.py](../cli/run_codex_claude_mesh.py)

In the latest verified full run:

- `service-codex-001`
- `service-codex-002`
- `service-codex-003`

all reached:

- `status = stopped`
- `reason = max_turns_reached`

The final state files were:

- [.agent-mesh-runtime/state/services.json](../.agent-mesh-runtime/state/services.json)
- [.agent-mesh-runtime/state/processes.json](../.agent-mesh-runtime/state/processes.json)

The router also stopped normally with:

- `reason = all_services_done`

The log files for the finished run are:

- [.agent-mesh-runtime/logs/router.jsonl](../.agent-mesh-runtime/logs/router.jsonl)
- [.agent-mesh-runtime/logs/service-codex-001.jsonl](../.agent-mesh-runtime/logs/service-codex-001.jsonl)
- [.agent-mesh-runtime/logs/service-codex-002.jsonl](../.agent-mesh-runtime/logs/service-codex-002.jsonl)
- [.agent-mesh-runtime/logs/service-codex-003.jsonl](../.agent-mesh-runtime/logs/service-codex-003.jsonl)

## What Was Verified

The following are verified, not just intended:

- Codex can return `service_control_v1`
- Claude can return `service_control_v1`
- the adapter can validate and consume the schema for both
- spawn requests can be emitted from structured output
- Codex session resume works across turns
- a three-service Codex conversation can run to completion

## Current Philosophy Boundary

The implementation remains aligned with the current design boundary:

- `message` remains the runtime IPC unit
- `service_control_v1` is not part of the wire
- schema interpretation is an adapter concern
- kernel does not need to understand provider-native output

This keeps the microkernel-like separation cleaner than the earlier text-control approach.

## Known Weaknesses

The following are still true:

- resumed Codex turns cannot use CLI-enforced schema validation because `resume` lacks `--output-schema`
- schema validation is currently manual and specific to `service_control_v1`
- the demo still emits one rejected visible message toward `user.local` in the opening turn

## Likely Next Step

The next natural implementation step would be one of:

- add command logging that explicitly records `codex exec` vs `codex exec resume`
- add another service-local schema beyond `service_control_v1`
- remove the remaining `user.local` reply from the three-service demo seed
