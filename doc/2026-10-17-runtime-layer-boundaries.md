# Runtime Layer Boundaries

Date: 2026-10-17
Status: design note; no code changes yet.

## Problem

Stability defects (the 2026-06-25 daemon `KeyError` and the 2026-07-10 daemon
`TimeoutExpired` termination, plus audit findings F1/F2 in
`doc/2026-10-03-GOAL-BASED-MULTI-SESSION-WORKFLOW-AUDIT.md`) mix three concerns
inside one flat `src/` tree:

1. the minimal MINIX-style message-passing runtime,
2. goal-based orchestration (GoalManager/WorkerAgent, dispatch, schedules),
3. the CLI surface and daemon process.

The message-passing runtime itself is sound and must remain in its current
minimal form. Refactoring must therefore assign every future change to exactly
one layer instead of touching the core.

## Layer definitions

| Layer | Responsibility | Current modules | Change policy |
| --- | --- | --- | --- |
| L1 Kernel | Sessions as receiver processes, Message passing, payload files, SessionLog, endpoint cursors, durable store, auth, agent-facing message API of `agent_api.py`, envelopes | `model.py` (message/id parts), `store_defs.py`, `store.py`, `store_files.py`, `store_message.py`, `store_session_log.py`, `store_auth.py`, `agent_api.py`, `envelope.py` | Frozen semantics. Re-homing only. |
| L2 Goal orchestration | Goals, goal lifecycle and completion, role readiness projection, dispatch policy/liveness, schedules/resolvers, provider execution and remote handoff | `store_session.py`, `store_dispatch.py`, `store_dispatch_readiness.py`, `store_query.py`, `dispatch_policy.py`, `dispatch_projection.py`, `store_prompts.py`, `schedule_resolvers.py`, `agents.py` | Refactor target for F1/F2 and stability work. |
| L3 CLI | Command parsing, console, rendering, daemon loop and Lots | `cli.py`, `cli_console.py`, `cli_render.py`, `cli_workers.py` | Thin adapters. Goal-free commands depend only on L1. |

## Dependency direction

`L3 → L2 → L1` only. No upward imports, no cross-layer shortcuts, and no goal
vocabulary inside L1 modules. A mechanical import audit script enforces this
before any behavior change.

## L1 contract (explicit, code unchanged)

1. **Transaction ownership.** `store.py:_state_lock` (fcntl `state.lock`) is
   the single writer lock. Callers must hold it across each entire
   load → change → save transaction. Locking only the save is insufficient.
2. **Delete-on-save.** `store_files.py:_save_item_collection` removes records
   absent from the saved snapshot. This is correct only when the save is the
   latest snapshot taken inside a locked transaction.
3. **Append-only sidecars.** SessionLog segments and message payload files are
   outside delete-on-save; they only ever append.
4. **Goal-free core.** L1 semantics stay independent of Goals, roles, and
   provider execution. Dispatch readiness is a projection owned by L2.

## Crash attribution on current knowledge

- **2026-07-10 `TimeoutExpired`**: daemonic process died because one external
  provider turn raising `subprocess.TimeoutExpired` in `agents.py:_run_external`
  propagated through a Lot future into the daemon loop. L2 (provider execution)
  plus L3 (unhandled `future.result()`). L1 not at fault. Already patched:
  `agents.py` converts timeout/launch failures to `AgentError`; `cli_workers.py`
  isolates Lot exceptions.
- **2026-06-25 `KeyError: run-…`**: L2 mutations without the transaction lock
  (`store_session.py:set_session_active`, `update_goal`) interleaved with a
  locked lease save; delete-on-save removed the in-flight `dispatch_run`, and
  the commit path indexed it directly. L1 behaved as contracted; the violating
  transactions live in L2. Commit path now uses `.get()` plus
  `_recover_missing_lease_run_locked`.

The 06-25 class is the only failure mode that could look like an L1 defect.
Under the contract above it is not: L1 offered the lock and followed its save
semantics. The repair stays in L2 by serializing every store mutation.

## L1 non-touch policy

- No L1 semantics, formats, or file layouts change in this program of work.
  Package re-homing may move files but not behavior; the existing 77-test
  suite must pass unchanged at each step.
- The F1/F2 reproduction probes from the 2026-10-03 audit become permanent
  regression tests. Passing them from L2-only edits is the evidence that L1
  stays innocent; if any probe can only be fixed inside L1 modules, the L1
  design is revisited explicitly.

## Agent service identities (decided 2026-10-17)

Hired agent entities become non-login service accounts (the `www-data`
pattern): a real durable identity with permissions, without any login path.

- One service account per provider entity (`codex`, `claude`, `opencode`,
  remote instances, ...), auto-registered by runtime. Accounts carry:
  provider execution profile, capability classes
  (`manager-capable` / `worker-capable`), and dispatch priority.
- Non-login enforcement reuses the existing gate: `store_auth.py` already
  rejects accounts whose `status` is not `active`; service accounts use a
  distinct `status: "service"` and therefore never authenticate.
- **Roles stay L2 job classes** (`GoalManagerReview`, `WorkerWork`). Dispatch
  selects among service accounts whose capabilities match the role, in
  priority order; an unavailable provider falls through to the next entry.
- Message senders keep the existing `agent:<name>` endpoint space; the name is
  the service account name instead of the role name. No envelope format change.
- Agent threads and resume tokens key per (session, account) instead of
  (session, role). Different provider accounts never share a thread context,
  which is also the structural fix for audit finding F3 (provider
  continuation): a provider thread belongs to exactly one service identity.
- GoalManager authority: only `manager-capable` accounts may receive
  GoalManagerReview; completion-declaration provenance is recorded as
  (service account, run lease). The current policy "remote is rejected for
  GoalManager" becomes a capability flag on the account, not a role rule.
- Migration: `init` creates service accounts matching today's defaults, and
  `agent_profiles`/`set-agent` are implemented as edits to the per-role
  service-account priority lists. With one capable account per role at the
  same priority, dispatch outcomes reproduce current behavior exactly.

Open points only when the first consumer arrives:

- Naming prefix or separate namespace so service accounts cannot collide with
  human usernames in the same account store.
- Whether the pool also allows concurrent WorkerAgent runs per session; the
  account model does not by itself add parallelism.

## Planned L2 repairs (after boundaries are in place)

Ordered after the 2026-10-03 audit; all remain inside L2:

1. P1: wrap every read/change/save mutation in the transaction lock
   (`set_session_active`, `update_goal`, and all remaining unlocked paths).
2. P1: fence completion and late results against the acquired run and the
   observed requirement window; record dated Goal body revisions in SessionLog.
3. P2: real provider continuation contract with persisted provider thread ids;
   verify resumption after restart before advertising provider Goals.
4. P2: bounded GoalManager waiting decisions that do not synthesize redundant
   Worker turns; event-or-timer wake readiness derived from the SessionLog.

## Privacy guarantee (personal profiles stay out of GitHub)

Verified 2026-10-17 against `origin = github.com/repyt-margorp/aize`:

- `.gitignore` covers `.aize-state/`, `.new-aize-state*`, `.temp/`; all runtime
  state, agent workspaces (including any browser/mail profile copies), and
  daemon logs therefore stay untracked.
- `git log --all -- .aize-state workspaces` is empty: no ref ever committed the
  runtime state.
- Pickaxe search over all refs for the personal identifiers observed in runtime
  prompts (account display names, mail domain, profile helper names, browser
  profile paths): zero findings in history and in the tracked tree.

Rules that keep the guarantee true:

1. Runtime state and agent workspaces under `.aize-state/**` remain
   untracked forever, including target-specific helpers and profile copies.
2. `src/`, `tests/`, `doc/`, and README must never contain personal
   identifiers, account names, hostnames, mail addresses, or
   workspace-specific absolute paths. Documentation quotes runtime data only
   through CLI history commands or with masked placeholders. This note obeys
   the same rule; the concrete identifier list is maintained locally.
3. Protection is local and untracked: a `.git/hooks/pre-push` script
   (never committed) greps outgoing commits against a personal-identifier
   pattern file stored outside the repository. Adding identifiers to the repo
   to describe the rule would defeat the rule.
4. Agent-dispatched doc work follows the same boundary: agents may read
   runtime state for auditing, but anything published to this repository
   passes a manual or hook-based identifier check before commit.

Remaining limits: repository visibility (public vs. private) was not verified
from this environment; the guarantee above holds for repository content, not
for any future external mirrors.

## Migration sequence

1. Re-home `src/` into `kernel/`, `goal/`, `cli/` packages with no behavior
   change; full test suite passes.
2. Add the dependency-direction import audit to the scripts and run it in CI
   or locally before every commit.
3. Implement the L2 repairs list; land each with its permanent regression test.
4. Only then consider higher-level runtime features on top of the unchanged
   L1 core.
