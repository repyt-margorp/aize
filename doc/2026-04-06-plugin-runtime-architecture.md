# Unit Package Runtime Architecture

## Goal

Keep the core Unit runtime publishable while allowing non-public userland units to live outside `src/services/`.

## Directory policy

Core services remain in `./src/services/`.

Repo-local unit packages live in `./plugins/`.

Private work lives in `./plugins/private/` and is ignored by Git.

Recommended unit package shape:

```text
plugins/
  private/
    accounting_suite/
      plugin.json
      services/
        journal_worker/
          __init__.py
          service.json
      units/
        journal/
          unit.json
```

## Runtime model

The Unit runtime already treats each service session as a process-like unit. This maps cleanly to a MINIX-style model:

1. `kernel` layer
   The kernel keeps the unit registry, process registry, message router, capability checks, identity, restart policy, and durable state indexes.

2. `unit` layer
   UnitFiles define ServiceUnits, SessionUnits, ManagerUnits, AgentUnits, InterfaceUnits, and DeviceUnits. Units communicate through endpoints and messages.

In practice:

- `service.json` defines executable service kinds.
- `unit.json` defines kernel-managed UnitFiles for humans, schedulers, and interfaces.
- `unit.json` may also declare unit-level persistence policy such as `"workspace_scope": "unit"` for durable unit-local files. Legacy `"app"` remains accepted as a compatibility alias.
- `spawn_requests` stays the runtime mechanism for creating new session processes.
- The current minimum UnitFile contract is documented in `./src/runtime/schemas/unit_file_v1.json`; `plugin_session_template_v1.json` remains as a compatibility filename.

## What changed

- Built-in service discovery still reads `./src/services/*/service.json`.
- Unit package discovery also reads `./plugins/**/services/*/service.json`.
- UnitFile discovery reads `./plugins/**/units/*/unit.json`.
- Legacy `session-templates/*/session-template.json` and `apps/*/app.json` descriptors are compatibility inputs.
- Service module import defaults to the service directory path relative to the repo root, so a unit package service under `plugins/private/acme/services/research_worker` resolves to module `plugins.private.acme.services.research_worker`.

## Operational guidance

- Keep shared infrastructure in core `src/`.
- Put sensitive business logic in `plugins/private/<unit-package>/services/...`.
- Use UnitFiles as the stable user-facing entrypoint for starting domain-specific sessions.
- Keep service `kind` values globally unique across core and plugins.
- If a unit package needs secrets, load them through environment variables or runtime config, not committed files.

## Next step

The next useful increment is to make old `session_template` implementation names internal-only compatibility shims and move public API callers to `/units`.
