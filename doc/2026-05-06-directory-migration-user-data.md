# Directory Migration and User Data

When moving an AIze checkout to a new directory, do not treat source files,
durable user state, and runtime identity as one undifferentiated copy.  A
directory move can appear successful while the new checkout still starts with
fresh account/session state or a new P2P identity.

This note records the migration procedure to use when a checkout is moved, for
example from `<old-repo>` to `<new-repo>`.

## State Classes

AIze currently has three state classes that need different handling:

- Source tree: tracked repository files under the checkout.
- Durable user state: `./.aize-state/`, including users, auth sessions, SessionUnit state,
  timelines, Unit definitions, and Unit workspaces.
- Runtime state: `./.aize-runtime/`, including generated manifests, process state, TLS
  certs, logs, and node identity.

Do not overwrite all of `./.aize-state/persistent.json` from the old checkout
without checking it.  It contains user and auth-session records.  If the new
checkout already has the intended passwords or active login sessions, preserve
the new auth data and migrate only the missing durable session/unit data.

## Required Preflight

Stop the old runtime before starting the new one.  Otherwise the old runtime can
keep the HTTP port and make the new checkout look healthy while it is actually
serving old runtime state.

Check for old runtime processes with a repo-relative pattern:

```bash
pgrep -a -f '<old-repo>/.aize-runtime' || true
pgrep -a -f '<new-repo>/.aize-runtime' || true
```

Back up the new state before copying anything:

```bash
mkdir -p <new-repo>/.temp/migration-backup
cp -a <new-repo>/.aize-state <new-repo>/.temp/migration-backup/aize-state.before
cp -a <new-repo>/.aize-runtime <new-repo>/.temp/migration-backup/aize-runtime.before
```

## Session and Unit Data

For each user being migrated, compare the old and new session directories before
copying:

```bash
find <old-repo>/.aize-state/sessions/<username> -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort
find <new-repo>/.aize-state/sessions/<username> -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort
```

Then copy missing or intentionally replaced session data:

```bash
cp -a <old-repo>/.aize-state/sessions/<username>/. <new-repo>/.aize-state/sessions/<username>/
```

Also migrate Unit definitions and Unit workspaces if they exist:

```bash
cp -a <old-repo>/.aize-state/units/<username>/. <new-repo>/.aize-state/units/<username>/
```

If the old Unit workspace directory does not exist, do not leave the Unit JSON
pointing at the old path.  Update `workspace_path` and `launcher_workspace_path`
fields to the new checkout path, and ensure the target workspace directory
exists.

## Auth Data

Keep password/auth data from the checkout that should remain authoritative.

If the new checkout already has the correct account credentials, do not replace
`<new-repo>/.aize-state/persistent.json` wholesale.  Instead, update only
non-auth migration fields such as `_runtime_root` when needed.

After migration, verify:

```bash
python3 - <<'PY'
import json
from pathlib import Path
data = json.loads(Path('.aize-state/persistent.json').read_text())
print('users:', sorted(data.get('users', {})))
print('auth_sessions_count:', len(data.get('auth_sessions', {})))
print('_runtime_root:', data.get('_runtime_root'))
PY
```

## Runtime Identity

For P2P continuity, preserve the old node identity unless the migration is
intended to create a new node.

Copy these files from the old runtime to the new runtime:

```bash
cp -a <old-repo>/.aize-runtime/identity/. <new-repo>/.aize-runtime/identity/
cp -a <old-repo>/.aize-runtime/state/node_id <new-repo>/.aize-runtime/state/node_id
```

Do not copy generated process state blindly.  Files such as
`./.aize-runtime/state/processes.json`, `./.aize-runtime/state/services.json`,
`./.aize-runtime/manifest.json`, and transient logs are regenerated at boot.

TLS certs can be copied when preserving browser trust is useful, but they should
be regenerated if host names or IP SANs changed.

## Path Reference Cleanup

After copying state, search durable JSON files for old checkout paths:

```bash
grep -R '<old-repo>' -n <new-repo>/.aize-state --exclude='*.jsonl' --exclude='*.tmp' || true
```

Update JSON fields that point to the old checkout.  Do not rewrite historical
timeline files unless a runtime reader depends on them; timeline entries are
audit history and may legitimately mention old paths.

## Restart and Verification

Start the new runtime with an explicit runtime root so shell environment
variables cannot accidentally point back to the old checkout:

```bash
cd <new-repo>
AIZE_RUNTIME_ROOT="$PWD/.aize-runtime" AIZE_HTTP_HOST=0.0.0.0 ./restart_aize_unit.sh
```

Verify that the running process uses the new runtime root:

```bash
pgrep -a -f "$PWD/.aize-runtime"
pgrep -a -f '<old-repo>/.aize-runtime' || true
```

Verify health using the configured port:

```bash
python3 - <<'PY'
import os
import ssl
import urllib.request

port = os.environ.get('AIZE_HTTP_PORT', '64123')
ctx = ssl._create_unverified_context()
with urllib.request.urlopen(f'https://127.0.0.1:{port}/health', context=ctx, timeout=5) as response:
    print(response.status, response.read(500).decode())
PY
```

Finally, confirm that expected sessions and Unit workspaces appear under the new
checkout, and that the node id is the intended one:

```bash
find .aize-state/sessions/<username> -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort
find .aize-state/units/<username> -maxdepth 3 -type f -o -type d
cat .aize-runtime/state/node_id
```
