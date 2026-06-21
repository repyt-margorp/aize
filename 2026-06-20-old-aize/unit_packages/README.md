Unit packages live under `./unit_packages/`.

Use this tree for MINIX-style userland units that should not be mixed into the core `src/services/` set.

Recommended layout:

```text
unit_packages/
  private/
    my_unit_package/
      unit-package.json
      services/
        secret_worker/
          __init__.py
          service.json
      units/
        entrance/
          unit.json
```

Notes:

- Private or non-public work should live under `./unit_packages/private/`; that subtree is gitignored.
- Each unit package must include a `unit-package.json`.
- Service modules are auto-discovered from `./unit_packages/**/services/*/service.json`.
- Units are auto-discovered from `./unit_packages/**/units/*/unit.json`.
- Unit descriptors must use `units/*/unit.json`.
- Unit descriptor shape is documented in `./src/runtime/schemas/unit_file_v1.json`.
- If a service directory is importable from the repo root, its Python module path is derived automatically.
