# Kernel Auth And Spawn Authorization

## Summary

- Account and capability logic now lives in `src/kernel/auth.py`.
- `service.spawn` is no longer accepted purely because a service asked for it.
- Spawn requests now carry an `auth` context, and the kernel checks for the `spawn_service` capability.
- Dynamic service records now capture `owner_principal`, `owner_roles`, and `owner_capabilities`.
- The kernel now recognizes a minimal `service.*` control plane: start, stop, restart, reload, and status.
- `HttpBridge` still owns browser login/session UX, but it now acts as a credential carrier instead of the authority boundary.

## Current Model

- Bootstrap creates a `root` account with roles `root` and `superuser`.
- Normal users default to the `user` role.
- Effective capabilities are derived from roles plus any explicit capability list stored on the principal.
- The persistent DB stays in the existing `.aize-state/persistent.json` file for migration compatibility.

## Message Flow

1. `HttpBridge` authenticates the browser user and issues an `auth` context for outgoing prompts.
2. The prompt to `service-codex-001` carries `auth`, `conversation`, and the usual routing metadata.
3. If Codex emits a `service.spawn` control request, the adapter copies the same `auth` context onto that kernel-bound message.
4. `kernel.spawn.SpawnManager` authorizes the request before creating FIFOs or launching the child adapter.
5. The spawned service is registered with the originating principal as its AIze-layer owner.

## Known Gaps

- Reply routing for multi-talk concurrency is still FIFO-based on the HTTP side and should eventually move to `run_id` or explicit conversation keys.
- Login/session cookies still live in `HttpBridge`; only principal data and control-plane authorization moved kernel-side.
- Capability editing UI does not exist yet; the kernel model supports capabilities, but the browser only exposes bootstrap and normal user creation today.
