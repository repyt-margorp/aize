# Plugin Runtime Architecture

## Goal

Keep the core mesh publishable while allowing non-public microservices and session-session templates to live outside `src/services/`.

## Directory policy

Core services remain in `./src/services/`.

Repo-local extensions live in `./plugins/`.

Private work lives in `./plugins/private/` and is ignored by Git.

Recommended plugin shape:

```text
plugins/
  private/
    accounting_suite/
      plugin.json
      services/
        journal_worker/
          __init__.py
          service.json
      apps/
        journal_launcher/
          session-template.json
```

## Runtime model

The mesh already treats each service session as a process-like unit. This maps cleanly to a two-layer model:

1. `service` layer
   Microservices remain the execution primitive. They are spawned, supervised, restarted, and routed exactly like the current built-in services.

2. `app` layer
   An app is metadata plus launch policy. It does not replace services; it composes them. A session template can declare the session types it creates, default peers, policy, and the initial prompt template used to spawn those sessions.

In practice:

- `service.json` defines executable service kinds.
- `session-template.json` defines launch surfaces for humans or the HTTP bridge UI.
- `session-template.json` may also declare template-level persistence policy such as `"workspace_scope": "app"` for durable template-local files.
- `spawn_requests` stays the runtime mechanism for creating new session processes.
- The current minimum `session-template.json` contract is documented in `./src/runtime/schemas/plugin_session_template_v1.json`.

## What changed

- Built-in service discovery still reads `./src/services/*/service.json`.
- Plugin discovery now also reads `./plugins/**/services/*/service.json`.
- Session Template discovery now reads `./plugins/**/session-templates/*/session-template.json`.
- Service module import defaults to the service directory path relative to the repo root, so a plugin service under `plugins/private/acme/services/research_worker` resolves to module `plugins.private.acme.services.research_worker`.

## Operational guidance

- Keep shared infrastructure in core `src/`.
- Put sensitive business logic in `plugins/private/<plugin>/services/...`.
- Use session templates as the stable user-facing entrypoint for starting domain-specific sessions.
- Keep service `kind` values globally unique across core and plugins.
- If a plugin needs secrets, load them through environment variables or runtime config, not committed files.

## Next step

The next useful increment is to expose the discovered `session-template.json` catalog through the HTTP bridge so the browser UI can render launchable session apps directly.
