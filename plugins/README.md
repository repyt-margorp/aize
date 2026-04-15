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
          app.json
```

Notes:

- Private or non-public work should live under `./plugins/private/`; that subtree is gitignored.
- Each plugin must include a `plugin.json`.
- Service modules are auto-discovered from `./plugins/**/services/*/service.json`.
- App descriptors are auto-discovered from `./plugins/**/apps/*/app.json`.
- App descriptor shape is documented in `./src/runtime/schemas/plugin_app_v1.json`.
- If a service directory is importable from the repo root, its Python module path is derived automatically.
