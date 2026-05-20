# AIze Notes

## Restart Debug
- Use `./restart_aize_unit.sh` for normal restarts. This is the repo-root entrypoint for the synchronous restart flow.
- `./scripts/restart_aize_unit.sh` remains as the script wrapper variant, but the root script should be treated as canonical when operating from the repo root.
- Normal restart calls self-detach into a supervisor so restart can continue after the old Unit runtime is terminated.
- Restart diagnostics logs live under `.temp/restart-debug/` (runtime state — not tracked in source).
- Run `python3 scripts/diagnostics/probe_restart.py` from the repo root to capture a restart report.
- The report records launcher/router/adapter PID transitions, the active HttpBridge health URL resolved from runtime state (default `https://127.0.0.1:4123/health`), restart script stdout/stderr, and the tail of `.temp/restart-debug/launcher.log`.
- Reports are written to `.temp/restart-debug/logs/`.

## Scripts
Operational and diagnostic scripts live under `./scripts/`:

| Script | Purpose |
|---|---|
| `restart_aize_unit.sh` | Canonical repo-root restart entrypoint |
| `scripts/restart_aize_unit.sh` | Dispatch wrapper for detached synchronous restart |
| `scripts/diagnostics/probe_restart.py` | Restart diagnostics probe — records PID transitions and health |
| `scripts/check_codex_context_window.sh` | Inspect remaining context % for a Codex session |
| `scripts/check_claude_context_window.sh` | Inspect remaining context % for a Claude session |
| `scripts/register_user.sh` | Securely register a new user (see User Registration below) |

## Routing / Execution Policy
- Do not gate core runtime behavior on implicit text heuristics. If a SessionUnit mode promises
  a role or agent path, dispatch that role deterministically from explicit session settings.
- In particular, an Interactive Session with `communication_agent_enabled=true` must start both
  the InteractiveAgent and WorkerAgent for user input. Do not decide WorkerAgent dispatch from
  keyword matching, prompt classification, or "looks like work" checks.
- If conditional behavior is needed, add an explicit persisted setting or schema field and document
  that setting. Avoid hidden fallback behavior that makes runtime state hard to reason about.

## User Registration
The web `/register` endpoint requires a superuser session. To create users without exposing passwords through the web UI, use the interactive script:

```
./scripts/register_user.sh [new-username]
```

The script will prompt for:
1. **Admin username** — a superuser account already in the system
2. **Admin password** — verified interactively (hidden input, never in command args)
3. **New user's password** — entered and confirmed interactively (hidden input)

Passwords are never passed as command-line arguments and do not appear in process listings. The script authenticates the admin first, then registers the new user using that session — so both the admin credential and new-user credential are verified before any account is created.

**Prerequisites:** the Unit runtime must be running (the active HttpBridge health URL should be reachable; the default is `https://127.0.0.1:4123/health`) and the admin account must have the `superuser` role.

## HTTPS / TLS Setup

The AIze Unit runtime runs on **HTTPS by default** using a self-signed certificate (オレオレ認証)
for the Web UI. Cert generation is handled automatically on start, but can also be run manually.

### Certificate location

Certificates are stored under the runtime directory (not tracked in source):

```
.aize-runtime/tls/server.crt   # self-signed certificate (PEM)
.aize-runtime/tls/server.key   # private key (PEM)
```

### Initial setup — generate the certificate

Run from the repo root before the first start (or any time the cert needs to be regenerated):

```bash
PYTHONPATH=./src python3 -m tls.gen_self_signed_cert
```

Optional flags:

| Flag | Default | Description |
|---|---|---|
| `--cert PATH` | `$AIZE_RUNTIME_ROOT/tls/server.crt` | Output certificate path |
| `--key PATH` | `$AIZE_RUNTIME_ROOT/tls/server.key` | Output private key path |
| `--days N` | `397` | Certificate validity in days, capped at 397 for browser compatibility |
| `--cn NAME` | `localhost` | Common Name (CN) field |
| `--no-auto-hosts` | unset | Do not add local interface IPv4/IPv6 addresses and host names to the SAN |
| `--hosts HOST...` | auto-discovered local hosts | Additional DNS names or IP addresses to add to the SAN |

The script adds a Subject Alternative Name (SAN) covering `DNS:localhost`, `IP:127.0.0.1`,
local host names, and usable local interface IPv4/IPv6 addresses. This lets the same Web UI
certificate work for loopback, LAN access, and direct IPv6 access when the browser permits
self-signed certificates. If the cert/key are absent, too long-lived, or missing required SANs
at startup, the adapter regenerates them automatically.

### Implementation

- **`src/tls/gen_self_signed_cert.py`** — cert generation logic (callable as a module or standalone script).
- **`src/runtime/cli_service_adapter.py`** — reads TLS config, wraps the `ThreadingHTTPServer` socket
  with an `ssl.SSLContext` before starting the server thread.

### Disabling TLS (development only)

Set the environment variable before restart:

```bash
AIZE_TLS=false ./restart_aize_unit.sh
```

Or add `"tls_enabled": false` to the `config` block of `service-http-001` in `manifest.json`.

### Custom cert paths

```bash
AIZE_TLS_CERT=/path/to/server.crt AIZE_TLS_KEY=/path/to/server.key ./restart_aize_unit.sh
```

### Custom SAN hosts

```bash
AIZE_TLS_HOSTS=example.local,2001:db8::10 ./restart_aize_unit.sh
```

Set `AIZE_TLS_AUTO_HOSTS=false` to disable automatic local IPv4/IPv6 and hostname discovery.

### Trusting the self-signed cert in a browser

Import `.aize-runtime/tls/server.crt` into your browser's trust store
(or OS keychain) to remove the "Not secure" warning.

### Scripts / health checks

`restart_aize_unit.sh` uses `curl -k` to skip cert verification when polling the
active HttpBridge health URL (default `https://127.0.0.1:4123/health`). `scripts/diagnostics/probe_restart.py` does the same
via a custom `ssl.SSLContext` with `CERT_NONE`.

## HTTPBridge Responsiveness
- Treat HttpBridge list views and session-detail views as latency-sensitive surfaces.
- Shape persistent state so top-level views can answer from session metadata or other immediately adjacent state first, then descend into timeline or runtime-log files only when the user opens a detail view.
- When adding filtering for "last N hours" or "last N days", prefer queryable timestamps and shallow indexes over pre-rendered summary blobs or full-file scans on every request.
- Refactors that affect HttpBridge must preserve quick initial render for SessionMap, WorkspaceView, and SessionLog before adding richer UI detail.

## Temporary Workspace
- Store temporary code, ad hoc scripts, scratch files, and throwaway test code under `./.temp/`.
- Do not create new temporary work under `./temp/`; use `./.temp/` instead.

## Documentation Notes
- When implementation notes or design records are needed, write them under `./doc/` using the filename format `yyyy-mm-dd-topic.md`.
- After code changes, add or update a concise implementation log under `./doc/` describing the user-visible behavior changed, files touched, verification run, and any remaining risk.

## Skill Positioning
- Session skills describe durable session conventions: routing expectations, workflow boundaries, verification requirements, and recurring operating norms for a SessionUnit.
- AdaptiveSkill content should hold reusable task code or short procedural helpers that may be invoked as needed for repeated work inside a session. Keep one-off implementation decisions in the session/doc log instead of baking them into broad session behavior.
- Do not use skills to silently reduce functionality. If a skill-guided refactor changes behavior, preserve visible capabilities unless the task explicitly asks to remove them, and record the behavioral change in `./doc/`.

## Environment-Independent Writing Policy
- **Never hard-code environment-specific data** in source files, scripts, or docs. This includes:
  - Absolute paths (e.g. `/home/<user>/...`) — use repo-relative paths (`./foo`, `../foo`) or shell variables (`$HOME`, `$(dirname "$0")`) instead.
  - Usernames, real names, or personal identifiers — use pseudonyms or placeholders if a name is needed at all.
  - Machine-specific hostnames, local IP addresses other than `127.0.0.1`/`localhost`, or port numbers that vary per environment (document the default; don't embed a personal override).
  - Local tool installation paths or shell profile specifics.
- Code and docs must be safe to publish to a public repository as written, without post-processing.
