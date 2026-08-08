# AIze

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
- per-session Agent workspaces
- per-Unit shared workspaces linked from Unit-derived Sessions
- CLI dispatch with durable run records
- Daemon-owned dispatch for queued Active/Incomplete Sessions
- CLI-only status inspection
- no UI

Official display name is `AIze`. Lowercase identifiers such as the `aize`
console command, `.aize-state`, `<aize-message>`, and `remote-aize` are stable
CLI/protocol names rather than brand spelling.

The previous runtime source is preserved under `2026-06-20-old-aize/` for
reference. The active CLI runtime lives directly under `src/`.

## Quick Start

From this repository, use the included launcher. It supplies the source path
and connects to this checkout's `.aize-state` by default:

```bash
./aize console
./aize status
```

Pass `--root` when operating a different runtime state directory:

```bash
./aize --root .other-aize-state console
```

The equivalent direct Python invocation is:

```bash
PYTHONPATH=src python3 -m cli --root .aize-state console
```

Other commands can also run directly from the repo without installing:

```bash
PYTHONPATH=src python3 -m cli --root .aize-state init
PYTHONPATH=src python3 -m cli --root .aize-state create-session notes
PYTHONPATH=src python3 -m cli --root .aize-state send root user kernel "hello"
PYTHONPATH=src python3 -m cli --root .aize-state send-file root user kernel ./README.md --body "readme snapshot"
PYTHONPATH=src python3 -m cli --root .aize-state recv kernel
PYTHONPATH=src python3 -m cli --root .aize-state status
PYTHONPATH=src python3 -m cli --root .aize-state accounts
PYTHONPATH=src python3 -m cli --root .aize-state agents
PYTHONPATH=src python3 -m cli --root .aize-state auth root root
PYTHONPATH=src python3 -m cli --root .aize-state sessions
PYTHONPATH=src python3 -m cli --root .aize-state session-graph
PYTHONPATH=src python3 -m cli --root .aize-state dispatch-once
PYTHONPATH=src python3 -m cli --root .aize-state units
PYTHONPATH=src python3 -m cli --root .aize-state messages root
```

Or install the CLI command:

```bash
python3 -m pip install -e .
aize --root .aize-state status
```

`init` always creates the default `root` Unit and the singleton `root` Session.
It also creates the default local `root` account with password `root`.
Additional sessions default to the system `root` Session as parent. A Session
does not need a Unit. A Session can also have multiple parents by passing
`--parent` more than once:

```bash
PYTHONPATH=src python3 -m cli --root .aize-state create-session task --parent root --parent notes
```

Each Session receives a workspace under the runtime state root:

```text
.aize-state/workspaces/sessions/{session-id}-{hash}/
```

External GoalManager and WorkerAgent providers run with that Session workspace
as their process working directory. The same path is also exposed to Agents as
`AIZE_SESSION_WORKSPACE`.

Each Unit also receives a shared workspace:

```text
.aize-state/workspaces/units/{unit-id}-{hash}/
```

When a Session is created from a Unit, the Session workspace contains a
`unit-workspace` symlink to that Unit workspace. External Agents also receive
the absolute Unit workspace path as `AIZE_UNIT_WORKSPACE`.

## Units / Session Templates

In this CLI runtime, a Unit is the durable executable-like definition used to
start Sessions. It can carry the default SessionGoal body, an initial prompt,
and a schedule. The Scheduler only uses `schedule.next_run_at` to decide when a
Unit is due.

```bash
PYTHONPATH=src python3 -m cli --root .aize-state create-unit monitor \
  --display-name "System Monitor" \
  --goal-text "Inspect system state and report findings." \
  --initial-prompt "Run diagnostics now." \
  --schedule-resolver next_interval_boundary \
  --schedule-fixed-parameters '{"interval_seconds":21600,"anchor":"scheduled_for"}' \
  --schedule-next-run-at "2026-06-22T00:00:00Z"

PYTHONPATH=src python3 -m cli --root .aize-state run-scheduled-units
```

Update an existing Unit without recreating it:

```bash
./aize configure-unit-schedule monitor next_interval_boundary \
  --fixed-parameters '{"interval_seconds":21600,"anchor":"scheduled_for"}'
```

`run-scheduled-units` starts Unit Sessions whose `schedule.next_run_at` is due.
The created Session receives the Unit `goal_text` as its SessionGoal. If
`initial_prompt` is set, it is recorded as User input on that Session so normal
Session dispatch can process it. When GoalManager completes a scheduled
Session, AIze invokes the Unit's schedule resolver with two separate inputs:
the Unit's fixed parameters and runtime parameters derived by AIze. Runtime
parameters include `scheduled_for`, `queued_at`, `started_at`, `completed_at`,
and optional call parameters from GoalManager. The resolver result becomes the
new `schedule.next_run_at`.

For normal operation, run the daemon. It initializes state if needed, polls due
Unit schedules, creates Sessions for due Units, and dispatches queued Session
work in the same foreground process:

```bash
PYTHONPATH=src python3 -m cli --root .aize-state daemon
```

The daemon runs until stopped. For service managers or tests, `--max-cycles`,
`--idle-timeout`, `--schedule-interval`, and `--dispatch-interval` can bound or
tune the loop.

Dispatch runs through interchangeable daemon Lots. Each Lot is only a worker
thread used to start one `dispatch_once` call; Sessions are not pinned to a
specific Lot. Start with a target Lot size:

```bash
PYTHONPATH=src python3 -m cli --root .aize-state daemon --dispatch-lots 10
```

The running daemon reads the target Lot size from state each cycle. Change it
from another shell with:

```bash
PYTHONPATH=src python3 -m cli --root .aize-state set-dispatch-lots 4
```

If the target is lowered below the number of currently running Codex processes,
the daemon lets those runs finish and only refills up to the new target. If the
target is raised, free Lots are filled on later cycles. Use
`--max-dispatch-lots` to cap how far a running daemon may grow.

Sessions have a user-controlled `active` flag. Goals have a GoalManager-owned
`completion_state` of `incomplete` or `complete`. Dispatch only selects Goals
that are both in an active Session and still incomplete.

```bash
PYTHONPATH=src python3 -m cli --root .aize-state deactivate-session task
PYTHONPATH=src python3 -m cli --root .aize-state activate-session task
```

## Login Console

Start the login console:

```bash
PYTHONPATH=src python3 -m cli --root .aize-state console
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
`dispatch` runs one role-specific work item. `GoalManager` runs
`GoalManagerReview`; `WorkerAgent` runs `WorkerWork` only after a Session
Message carries `payload.worker_request: true`. Each Session keeps durable agent
threads with resume tokens so later dispatches can continue the same
role-specific context.
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
PYTHONPATH=src python3 -m cli --root .aize-state set-agent GoalManager claude
PYTHONPATH=src python3 -m cli --root .aize-state set-agent WorkerAgent local
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
another AIze system through message passing:

```bash
PYTHONPATH=src python3 -m cli --root .aize-state set-agent WorkerAgent remote-aize
```

When `WorkerAgent` uses `remote-aize`, dispatch records both the WorkerAgent
turn and a `RemoteAIzeWorkerHandoff` Message addressed to `remote-aize`. The
handoff prompt is stored in a Message payload, so remote participation and
future file exchange stay inside the message-passing model.

Each Session stores capability metadata on the Session record. Capabilities are
included in Agent prompts, but they are not recorded as Session Messages.

Agent stdout/stderr is recorded on dispatch-run steps, not as Session Messages.
Only messages explicitly sent through the AIze Agent API are appended to
MessageLog. `send_user_console_message(...)` sends user-visible text to the
current console reply endpoint.

When user input is sent to a Session, the Session's latest Goal is marked
`incomplete` again and queued with user-input priority. If the Session has no
Goal yet, a default reply Goal is created. In the interactive console, `send`
records the UserInput Message and returns immediately; the running Daemon owns
dispatch. While the console stays open, it polls new `UserConsole` Messages for
the current Session and prints Agent replies as they arrive.

If a WorkerAgent run is already active for the Session, new UserInput is also
recorded as a WorkerAgent follow-up Message. That follow-up waits until the
current WorkerAgent run releases, then dispatch resumes the same WorkerAgent
thread with the updated Session MessageLog.

The interactive console never starts dispatch workers. Run the Daemon as the
single dispatcher. `dispatch` remains available as an explicit diagnostic or
manual operation when the Daemon is intentionally stopped.

For dispatch-only queue processing without schedule polling, run a foreground
dispatch worker in another shell:

```bash
PYTHONPATH=src python3 -m cli --root .aize-state dispatch-worker
```

The worker polls the dispatch scheduling index and dispatches new active,
incomplete Session Goals as they appear. Triggered entries point back to Session
Messages, so the Session MessageLog remains the main work stream.

## Scope

This is intentionally not a port of the existing AIze runtime. It is a clean,
small system that can be inspected from the CLI before higher-level runtime
features are added.
