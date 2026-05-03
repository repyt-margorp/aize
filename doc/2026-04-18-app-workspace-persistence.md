# Unit Workspace Persistence

## Summary

AIze previously persisted state at the session level, but it did not provide a durable filesystem workspace at the unit level.
That meant a UnitFile could repeatedly create useful sessions while still lacking a stable place to accumulate code,
scripts, prompts, notes, and artifacts across launches.

This change adds an optional **unit-scoped persistent workspace** for UnitFiles.

## Why This Was Needed

The immediate driver was a UnitFile that needed more than one-shot session startup behavior.
It needed a durable place to keep evolving operational code and supporting artifacts across multiple launches.

Session storage alone was not a good fit because:

- session directories are tied to one launched session, not the unit as a whole
- code and notes that should survive across launches become fragmented
- the launcher had no standard way to tell an agent where its durable unit-local code should live

## What This Adds

UnitFiles may now declare:

```json
"workspace_scope": "app"
```

When enabled, AIze creates a persistent workspace here:

```text
.aize-state/session-templates/<username>/<unit_id>/workspace/
```

It also records metadata in:

```text
.aize-state/session-templates/<username>/<unit_id>/unit.json
```

The launched session then receives:

- `launcher_workspace_scope`
- `launcher_workspace_path`

and the launch plan prepends a prompt note telling the agent to use that directory for durable unit-local assets.

## Design Intent

This is intentionally minimal.
It does not yet add a full unit-local file browser or unit-level ACL model.
Instead, it establishes the missing primitive: a stable, unit-owned workspace path that survives across launched sessions.

That primitive is enough to support durable unit code stock while keeping the existing session model intact.

## Impact

- UnitFiles can now keep durable code and artifacts without depending on one specific session directory
- unit behavior can evolve across launches without losing local context
- operational units now have a clean place to keep their unit-specific implementation state
