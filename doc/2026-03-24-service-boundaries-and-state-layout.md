# Service Boundaries And State Layout

## Summary

This refactor keeps the MINIX-style boot model introduced on 2026-03-23 and tightens one remaining boundary around persistent state placement.

- Boot now remains explicitly two-stage:
  - `src/cli/run_codex_http_mesh.py` starts only the router plus `service-svcmgr-001`
  - `service-svcmgr-001` expands service descriptors and spawns `HttpBridge`, Codex, Claude, and any restored dynamic services
- Runtime service definitions continue to live under `src/services/`
- Persistent session/auth state stays outside the disposable runtime root for the canonical repo runtime, but ephemeral runtimes now isolate their own state trees

## Why The State Layout Change Was Needed

The persistent state helpers had been changed to write durable state to a sibling `.aize-state/` directory beside the runtime root. That is correct for the canonical runtime:

- runtime root: `./.agent-mesh-runtime/`
- persistent state root: `./.aize-state/`

But that same rule caused collisions for temporary runtimes created under shared parents such as `/tmp`, because multiple ad hoc runs could converge on `/tmp/.aize-state/`.

The layout rule is now:

- if the runtime root is a canonical `.agent-mesh-runtime*` directory, use the sibling `.aize-state/`
- otherwise, place persistent state under `<runtime-root>/.aize-state/`

That keeps the production/runtime split intact while making tests, probes, and scratch runtimes self-contained.

## Code Impact

- `src/runtime/persistent_state_pkg/_core.py`
  - `state_dir(runtime_root)` now selects canonical sibling state only for `.agent-mesh-runtime*`
  - other runtimes get a colocated nested `.aize-state/`
- `src/runtime/persistent_state_pkg/conversation.py`
  - session scans now use `sessions_dir(runtime_root)` instead of rebuilding `.aize-state` paths by hand
- `tests/test_goal_manager_compact.py`
  - assertions now follow the public path helpers instead of hard-coding a specific filesystem layout

## Result

- Service startup remains service-manager-driven instead of CLI-driven
- Durable state paths are now deterministic and isolated per runtime shape
- Tests and diagnostic runtimes no longer depend on a writable shared `/tmp/.aize-state/`
