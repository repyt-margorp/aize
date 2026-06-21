# aize

Minimal AIze rebuild focused on a small MINIX-style core:

- explicit message passing
- file exchange through message payload files
- Session-centered User input messages
- XML-style AIze message envelopes in Agent prompts
- durable sessions and units
- default singleton root Unit and root Session
- default local root account
- session DAG links
- Session `Active` / `Inactive` user control
- GoalManager `Complete` / `Incomplete` evaluation
- per-session GoalManager and WorkerAgent threads
- CLI dispatch with durable run records
- console startup dispatch for queued Active/Incomplete Sessions
- CLI-only status inspection
- no UI

## Quick Start

Run directly from the repo without installing:

```bash
PYTHONPATH=src python3 -m new_aize.cli --root .new-aize-state init
PYTHONPATH=src python3 -m new_aize.cli --root .new-aize-state create-session notes
PYTHONPATH=src python3 -m new_aize.cli --root .new-aize-state send root user kernel "hello"
PYTHONPATH=src python3 -m new_aize.cli --root .new-aize-state send-file root user kernel ./README.md --body "readme snapshot"
PYTHONPATH=src python3 -m new_aize.cli --root .new-aize-state recv kernel
PYTHONPATH=src python3 -m new_aize.cli --root .new-aize-state status
PYTHONPATH=src python3 -m new_aize.cli --root .new-aize-state accounts
PYTHONPATH=src python3 -m new_aize.cli --root .new-aize-state agents
PYTHONPATH=src python3 -m new_aize.cli --root .new-aize-state auth root root
PYTHONPATH=src python3 -m new_aize.cli --root .new-aize-state sessions
PYTHONPATH=src python3 -m new_aize.cli --root .new-aize-state session-graph
PYTHONPATH=src python3 -m new_aize.cli --root .new-aize-state dispatch-once
PYTHONPATH=src python3 -m new_aize.cli --root .new-aize-state units
PYTHONPATH=src python3 -m new_aize.cli --root .new-aize-state messages root
```

Or install the CLI command:

```bash
python3 -m pip install -e .
aize --root .new-aize-state status
```

`init` always creates the default `root` Unit and the singleton `root` Session.
It also creates the default local `root` account with password `root`.
Additional sessions default to the system `root` Session as parent. A Session
does not need a Unit. A Session can also have multiple parents by passing
`--parent` more than once:

```bash
PYTHONPATH=src python3 -m new_aize.cli --root .new-aize-state create-session task --parent root --parent notes
```

Sessions have a user-controlled `active` flag. Goals have a GoalManager-owned
`completion_state` of `incomplete` or `complete`. Dispatch only selects Goals
that are both in an active Session and still incomplete.

```bash
PYTHONPATH=src python3 -m new_aize.cli --root .new-aize-state deactivate-session task
PYTHONPATH=src python3 -m new_aize.cli --root .new-aize-state activate-session task
```

## Login Console

Start the login console:

```bash
PYTHONPATH=src python3 -m new_aize.cli --root .new-aize-state console
```

For the default local account, log in as `root` with password `root`.

Inside the console:

```text
session notes
use root
goal child-session BuildChild "child body"
send "hello from root session"
dispatch
use child-session
messages
goals
agent-threads
dispatch-runs
exit
```

Console commands print compact human-readable summaries. The non-interactive
commands still print JSON so scripts can keep parsing them.

`session SESSION` creates a child Session under the currently selected Session
without attaching it to a Unit.
`unit-session SESSION UNIT` creates a child Session from a specific Unit.
`goal CHILD_SESSION TITLE [BODY]` creates a child Session under the currently
selected Session and starts an active Goal in that child Session.
`unit-goal CHILD_SESSION UNIT TITLE [BODY]` does the same from a specific Unit.
`send BODY` records User input as one queued Message addressed to the selected
`Session`. Dispatch then passes the selected Session messages to `GoalManager`
and `WorkerAgent` in an XML-style `<aize-message-bundle>`.
`send-file SESSION SENDER RECIPIENT PATH` reads a local text file and carries
its contents as a Message `payload.files` entry. It does not copy files through a
side channel or require shared filesystem access.
`dispatch` runs one active Goal through a per-Session `GoalManager` precheck,
`WorkerAgent` work step, and `GoalManager` completion check. Each Session keeps
durable agent threads with resume tokens so later dispatches can continue the
same role-specific context.
Dispatch occupancy is recorded as a dispatch-run lease history (`lease_state`,
`lease_acquired_at`, `lease_released_at`) rather than as a durable Goal or
Session progress flag.

By default both agent roles use the `codex` provider:

```text
GoalManager -> codex
WorkerAgent  -> codex
```

To assign another provider:

```bash
PYTHONPATH=src python3 -m new_aize.cli --root .new-aize-state set-agent GoalManager claude
PYTHONPATH=src python3 -m new_aize.cli --root .new-aize-state set-agent WorkerAgent local
```

External provider subprocess execution is enabled by default. Set
`AIZE_ENABLE_EXTERNAL_AGENTS=false` before `dispatch` to record and resume the
durable Session thread without launching an external process.
When `codex` is externally executed, AIze invokes `codex exec` with
`--sandbox danger-full-access` and
`--dangerously-bypass-approvals-and-sandbox` so dispatched Codex work can run
with full local permissions.

`GoalManager` must be evaluated on this PC. `remote-aize` is rejected for
`GoalManager`, but can be assigned to `WorkerAgent` to represent work handed to
another AIZE system through message passing:

```bash
PYTHONPATH=src python3 -m new_aize.cli --root .new-aize-state set-agent WorkerAgent remote-aize
```

When `WorkerAgent` uses `remote-aize`, dispatch records both the WorkerAgent
turn and a `RemoteAizeWorkerHandoff` Message addressed to `remote-aize`. The
handoff prompt is stored in a Message payload, so remote participation and
future file exchange stay inside the message-passing model.

Each Session stores capability metadata on the Session record. Capabilities are
included in Agent prompts, but they are not recorded as Session Messages.

Agent stdout/stderr is recorded on dispatch-run steps, not as Session Messages.
Only messages explicitly sent through the AIZE Agent API are appended to
MessageLog. `send_user_console_message(...)` sends user-visible text to the
current console reply endpoint.

When user input is sent to a Session, the Session's latest Goal is marked
`incomplete` again and queued with user-input priority. If the Session has no
Goal yet, a default reply Goal is created. In the interactive console, `send`
records the UserInput Message, starts a one-shot background dispatch worker for
that Session, and returns the prompt without waiting for Agent execution.
While the console stays open, it polls new `UserConsole` Messages for the
current Session and prints Agent replies as they arrive.

When the interactive console starts, it also checks for queued Active/Incomplete
SessionGoals. If queued work exists and no dispatch lease is currently acquired,
the console starts a detached background worker with a recovery context. That
context is stored on the dispatch-run record and passed to GoalManager and
WorkerAgent prompts; it is not appended to MessageLog.

For automatic queue processing, run a foreground dispatch worker in another
shell:

```bash
PYTHONPATH=src python3 -m new_aize.cli --root .new-aize-state dispatch-worker
```

The worker polls the priority `dispatch_queue` and dispatches new active,
incomplete Session Goals as they appear.

## Scope

This is intentionally not a port of the existing AIze runtime. It is a clean,
small system that can be inspected from the CLI before higher-level runtime
features are added.
