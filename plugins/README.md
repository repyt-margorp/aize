Plugin packages live under `./plugins/`.

Use this tree for service-scoped extensions that should not be mixed into the core `src/services/` set.

Recommended layout:

```text
plugins/
  private/
    my_plugin/
      plugin.json
      services/
        secret_worker/
          __init__.py
          service.json
      apps/
        launcher/
          session-template.json
```

Notes:

- Private or non-public work should live under `./plugins/private/`; that subtree is gitignored.
- Each plugin must include a `plugin.json`.
- Service modules are auto-discovered from `./plugins/**/services/*/service.json`.
- Session Template descriptors are auto-discovered from `./plugins/**/session-templates/*/session-template.json`.
- Session Template descriptor shape is documented in `./src/runtime/schemas/plugin_session_template_v1.json`.
- Launcher apps may set `"workspace_scope": "app"` when they need a durable template-level workspace across launches.
- If a service directory is importable from the repo root, its Python module path is derived automatically.
