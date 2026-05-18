Unit packages live under `./plugins/`.

Use this tree for MINIX-style userland units that should not be mixed into the core `src/services/` set.

Recommended layout:

```text
plugins/
  private/
    my_unit_package/
      plugin.json
      services/
        secret_worker/
          __init__.py
          service.json
      units/
        entrance/
          unit.json
```

Notes:

- Private or non-public work should live under `./plugins/private/`; that subtree is gitignored.
- Each unit package must include a `plugin.json`; the filename is retained as the package manifest for compatibility.
- Service modules are auto-discovered from `./plugins/**/services/*/service.json`.
- UnitFiles are auto-discovered from `./plugins/**/units/*/unit.json`.
- Legacy `session-templates/*/session-template.json` and `apps/*/app.json` descriptors are still accepted as compatibility input.
- Unit descriptor shape is documented in `./src/runtime/schemas/unit_file_v1.json`; `plugin_session_template_v1.json` remains as a compatibility filename.
- UnitFiles may set `"workspace_scope": "unit"` when they need a durable unit-level workspace across launches. Legacy `"app"` remains accepted as a compatibility alias.
- If a service directory is importable from the repo root, its Python module path is derived automatically.
