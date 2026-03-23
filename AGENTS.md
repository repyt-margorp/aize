# Aize Notes

## Restart Debug
- Use `./restart_codex_http_mesh.sh` for normal restarts. This is the repo-root entrypoint for the synchronous restart flow.
- `./scripts/restart_codex_http_mesh.sh` remains as the script wrapper variant, but the root script should be treated as canonical when operating from the repo root.
- Normal restart calls self-detach into a supervisor so restart can continue after the old mesh is terminated.
- Restart diagnostics logs live under `.temp/restart-debug/` (runtime state — not tracked in source).
- Run `python3 scripts/diagnostics/probe_restart.py` from the repo root to capture a restart report.
- The report records launcher/router/adapter PID transitions, `https://127.0.0.1:4123/health`, restart script stdout/stderr, and the tail of `.temp/restart-debug/launcher.log`.
- Reports are written to `.temp/restart-debug/logs/`.

## Scripts
Operational and diagnostic scripts live under `./scripts/`:

| Script | Purpose |
|---|---|
| `restart_codex_http_mesh.sh` | Canonical repo-root restart entrypoint |
| `scripts/restart_codex_http_mesh.sh` | Dispatch wrapper for detached synchronous restart |
| `scripts/diagnostics/probe_restart.py` | Restart diagnostics probe — records PID transitions and health |
| `scripts/check_codex_context_window.sh` | Inspect remaining context % for a Codex session |
| `scripts/check_claude_context_window.sh` | Inspect remaining context % for a Claude session |
| `scripts/register_user.sh` | Securely register a new user (see User Registration below) |

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

**Prerequisites:** the mesh must be running (`https://127.0.0.1:4123/health` should be reachable) and the admin account must have the `superuser` role.

## HTTPS / TLS Setup

The HTTP mesh runs on **HTTPS by default** using a self-signed certificate (オレオレ認証).
Cert generation is handled automatically on first start, but can also be run manually.

### Certificate location

Certificates are stored under the runtime directory (not tracked in source):

```
.agent-mesh-runtime/tls/server.crt   # self-signed certificate (PEM)
.agent-mesh-runtime/tls/server.key   # private key (PEM)
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
| `--days N` | `3650` | Certificate validity in days |
| `--cn NAME` | `localhost` | Common Name (CN) field |

The script adds a Subject Alternative Name (SAN) covering `DNS:localhost` and `IP:127.0.0.1`
so the cert is accepted by Python's `ssl` module and modern browsers.
If the cert/key are absent at startup the adapter generates them automatically.

### Implementation

- **`src/tls/gen_self_signed_cert.py`** — cert generation logic (callable as a module or standalone script).
- **`src/runtime/cli_service_adapter.py`** — reads TLS config, wraps the `ThreadingHTTPServer` socket
  with an `ssl.SSLContext` before starting the server thread.

### Disabling TLS (development only)

Set the environment variable before restart:

```bash
AIZE_TLS=false ./restart_codex_http_mesh.sh
```

Or add `"tls_enabled": false` to the `config` block of `service-http-001` in `manifest.json`.

### Custom cert paths

```bash
AIZE_TLS_CERT=/path/to/server.crt AIZE_TLS_KEY=/path/to/server.key ./restart_codex_http_mesh.sh
```

### Trusting the self-signed cert in a browser

Import `.agent-mesh-runtime/tls/server.crt` into your browser's trust store
(or OS keychain) to remove the "Not secure" warning.

### Scripts / health checks

`restart_codex_http_mesh.sh` uses `curl -k` to skip cert verification when polling
`https://127.0.0.1:4123/health`.  `scripts/diagnostics/probe_restart.py` does the same
via a custom `ssl.SSLContext` with `CERT_NONE`.

## Temporary Workspace
- Store temporary code, ad hoc scripts, scratch files, and throwaway test code under `./.temp/`.
- Do not create new temporary work under `./temp/`; use `./.temp/` instead.

## Documentation Notes
- When implementation notes or design records are needed, write them under `./doc/` using the filename format `yyyy-mm-dd-topic.md`.

## Environment-Independent Writing Policy
- **Never hard-code environment-specific data** in source files, scripts, or docs. This includes:
  - Absolute paths (e.g. `/home/<user>/...`) — use repo-relative paths (`./foo`, `../foo`) or shell variables (`$HOME`, `$(dirname "$0")`) instead.
  - Usernames, real names, or personal identifiers — use pseudonyms or placeholders if a name is needed at all.
  - Machine-specific hostnames, local IP addresses other than `127.0.0.1`/`localhost`, or port numbers that vary per environment (document the default; don't embed a personal override).
  - Local tool installation paths or shell profile specifics.
- Code and docs must be safe to publish to a public repository as written, without post-processing.
