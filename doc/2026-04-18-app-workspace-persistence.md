# Session Template Workspace Persistence

## Summary

AIZE previously persisted state at the session level, but it did not provide a durable filesystem workspace at the app level.
That meant a session template could repeatedly create useful sessions while still lacking a stable place to accumulate code,
scripts, prompts, notes, and artifacts across launches.

This change adds an optional **app-scoped persistent workspace** for session templates.

## Why This Was Needed

The immediate driver was a session template that needed more than one-shot session startup behavior.
It needed a durable place to keep evolving operational code and supporting artifacts across multiple launches.

Session storage alone was not a good fit because:

- session directories are tied to one launched session, not the app as a whole
- code and notes that should survive across launches become fragmented
- the launcher had no standard way to tell an agent where its durable template-local code should live

## What This Adds

Launcher apps may now declare:

```json
"workspace_scope": "app"
```

When enabled, AIZE creates a persistent workspace here:

```text
.aize-state/session-templates/<username>/<template_id>/workspace/
```

It also records metadata in:

```text
.aize-state/session-templates/<username>/<template_id>/session-template.json
```

The launched session then receives:

- `launcher_workspace_scope`
- `launcher_workspace_path`

and the launch plan prepends a prompt note telling the agent to use that directory for durable template-local assets.

## Design Intent

This is intentionally minimal.
It does not yet add a full template-local file browser or template-level ACL model.
Instead, it establishes the missing primitive: a stable, app-owned workspace path that survives across launched sessions.

That primitive is enough to support durable app code stock while keeping the existing session model intact.

## Impact

- session templates can now keep durable code and artifacts without depending on one specific session directory
- app behavior can evolve across launches without losing local context
- operational apps now have a clean place to keep their app-specific implementation state
