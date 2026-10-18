# Runtime Layer Boundaries

Date: 2026-10-17
Status: implemented and verified. The migration program below was executed on
this date: package re-home with an unchanged behavior suite, a static
layer-boundary audit, and the L2 repairs list (transaction locks, completion
fencing, provider thread contract, bounded waiting). See "Implementation
record" under the Migration sequence.

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

Final module layout after the 2026-10-17 implementation:

| Layer | Responsibility | Modules | Change policy |
| --- | --- | --- | --- |
| L1 Kernel | Sessions as receiver processes, Message passing, payload files, SessionLog segments, endpoint cursors, durable storage engine plus the fcntl state lock, auth | `src/kernel/{defs,model,storage,lock,auth,messages,session_log,envelope}.py`; top-level `src/agent_api.py` remains the stable external message API surface for agent subprocesses | Frozen semantics; storage schemas are supplied by the composition root. |
| L2 Goal orchestration | Goal lifecycle and completion, bounded waiting, role readiness projection, dispatch policy/liveness, schedules/resolvers, prompts, provider execution and remote handoff, the concrete `Store` facade composition | `src/goal/{defs,model,sessions,dispatch,readiness,policy,projections,prompts,schedules,agents,query,log,messages,store}.py` | Refactor target for F1/F2 and stability work. |
| L3 CLI | Command parsing, console, rendering, daemon loop and Lots | `src/cli/{app,console,render,workers}.py` | Thin adapters. Goal-free commands depend only on L1. |

## Dependency direction

`L3 → L2 → L1` only. No upward imports, no cross-layer shortcuts, and no goal
vocabulary inside L1 modules. A mechanical import audit script enforces this
before any behavior change.

## L1 contract (explicit, code unchanged)

1. **Transaction ownership.** `kernel/lock.py:StateLockMixin._state_lock`
   (fcntl `state.lock`) is the single writer lock. Callers must hold it across
   each entire load → change → save transaction. Locking only the save is
   insufficient.
2. **Delete-on-save.** `kernel/storage.py:_save_item_collection` removes records
   absent from the saved snapshot. This is correct only when the save is the
   latest snapshot taken inside a locked transaction. The collection-identity
   maps live with the composition root (`goal/store.py:STATE_ITEM_COLLECTIONS`,
   `STATE_VALUE_COLLECTIONS`), so the storage engine itself is goal-free.
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

## Agent service identities (decided 2026-10-17; implemented 2026-10-18)

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

(Implemented as the `svc-` reserved namespace with non-login `status:
"service"` users, and no concurrent pool change; see phase 3's implementation
record in `doc/2026-10-17-user-group-terminology.md`. The (session, account)
thread contract is realized as `(session, role, account)` keys so the two slots
of one session stay separate.)

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

## Implementation record (2026-10-17)

1. **Re-home.** All modules moved under `src/kernel/`, `src/goal/`, `src/cli/`;
   the external subprocess contract keeps `import agent_api` at the `src/`
   top level. `python3 -m cli` continues to work via the package `__main__`.
   The full behavior suite passed unchanged at every step (77 tests).
2. **Import audit.** `scripts/check_layer_imports.py` enforces
   `cli → goal → kernel` import directions and a goal-vocabulary ban inside
   `kernel/` modules; the exit code blocks commits on violation.
3. **P1 transaction coverage.** Every public mutating store operation now runs
   inside the fcntl state transaction (`@state_transaction` on the unlocked
   paths: `create_unit`, `run_scheduled_units`, `run_startup_units`,
   `set_agent_provider`, `set_session_active`, `create_session`,
   `start_goal_session`, `update_goal`, `link_sessions`, `receive_message`,
   `create_account`). Tests: `tests/test_store_transactions.py` instrumented
   save-vs-lock coverage plus concurrent write races.
4. **P1 completion fencing.** Dispatch runs record their observed goal body
   and log window at acquire. Declaration and final commit re-validate lease
   ownership and the observed window; stale results are retained as run
   evidence (`stale_results`, `StaleDispatchResultDiscarded` signal) without
   completing or advancing the SessionGoal, and replaced requirement
   work wakes a fresh review. Goal body updates are logged as dated
   `GoalBodyUpdated` SessionLog events. Tests: `tests/test_dispatch_fencing.py`.
5. **P2 provider thread contract.** `AgentResult` may carry
   `provider_thread_id`; the durable agent thread persists a per-provider
   `provider_threads` map and the next run receives the persisted ids after a
   process restart. Tests: `tests/test_provider_threads.py` including the
   restart assertion.
6. **P2 bounded waiting.** GoalManager may declare `set_goal_waiting` with a
   `deadline`, `event`, or `child` spec through its acquired run. The waiting
   decision leaves the SessionGoal incomplete while it suppresses implicit
   Worker requests and liveness recovery; wake clears the decision via a
   `GoalWaitingWake` signal derived from the log (deadline passage, a
   Session Message carrying `wake_events`, or a `ChildGoalCompleted` event),
   and new UserInput cancels the wait (`GoalWaitingCancelled`).
   `dispatch-liveness` reports `waiting_count` and treats a waiting Session as
   covered. Tests: `tests/test_goal_waiting.py`.

Kernel untouched claim per the probes: the two crash-mode repairs and the
F1/F2 regressions are covered by tests that pass with kernel modules only
moved, not modified.
