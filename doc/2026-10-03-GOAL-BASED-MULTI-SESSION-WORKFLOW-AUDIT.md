# Goal-Based Multi-Session Workflow Audit

Date: 2026-10-03
Status: audit complete; Core review verified. No implementation repairs authorized.
Repository baseline: `5d1072c1372b11fe878940ed271c5384fff1ca04` plus existing local edits.

## Problem List

1. Relate the observed A Program multi-goal workflow to aize's actual design.
2. Identify stability risks and a small, evidence-based improvement sequence.

## 1. Goal-Based Workflow and Project Fit

### Subjective (User)

2026-10-03, English paraphrase: this is completely separate from current A Program
development. Multi-goal development should belong in `aize`; create one additional
session, inspect `../aize`, and leave an audit under `../aize/doc` explaining what
can be learned from the current Goal-based development approach. Stable operation
of aize is difficult. Do not infer a particular failure cause from this statement.

2026-10-03, English paraphrase of the latest human clarification: publish the
completed AIze audit, end that work, then prioritize A Program.

### Objective (Code)

Fresh inspection, 2026-10-03: no applicable `AGENTS.md` exists in this checkout
or its ancestors; the archived runtime is outside this audit. Read current
`src/`, tests, local diffs against HEAD, README and relevant design notes.
HEAD remains `5d1072c1372b11fe878940ed271c5384fff1ca04`; 18 tracked files have
pre-existing edits and the dispatch-liveness note is untracked. Hashes below
pin the inspected and tested worktree, rather than treating HEAD as its contents.

| A Program practice | Current AIze fit and boundary |
| --- | --- |
| Separate task Goals and working directories | One current Goal per Session; `goal_id`, `session_id`, role thread key and `run_id` are distinct identities. Role work items are GoalManagerReview/WorkerWork, not independent long-term task records. Per-Session working directories isolate Sessions; both roles share that directory and Unit Sessions link the same Unit workspace. See `model.py:110`, `store_dispatch.py:127,825`, `store_session.py:60,84,973`. |
| Core owns integration; workers submit evidence | GoalManager alone declares completion through an acquired run; WorkerAgent sends Session reports. Worker reports wake review, stdout is retained on run steps, and neither a report nor exit 0 proves the Goal. The declaration contains state/reason, not mechanically verified acceptance criteria. See `store_session.py:1083`, `store_dispatch.py:235,499`, `store_message.py:175`, `dispatch_projection.py:141`. |
| Related Goals remain independently owned | Local edits add read-only Goal DAG context and ChildGoalCompleted events to every direct parent; completed parents reopen for review. This does not automatically complete parents or integrate source. See `store_query.py:287`, `store_session_log.py:207`, `store_session.py:1063`. |
| Notifications or six-hour wait while workers continue | Session reports, child events and SystemSignals provide durable wake input. However an incomplete GoalManager decision with no explicit request immediately creates Worker work; it cannot express Core's incomplete-but-waiting decision. Unit schedules create recurring Sessions, rather than a wait deadline on the existing coordinator Goal. See `store_dispatch.py:367,747`, `store_session.py:243`, `store_prompts.py:150`. |
| Pause, resume, cancellation and restart | Session `active` filters future dispatch; deactivation does not interrupt the running subprocess. Follow-up UserInput waits for the next Worker turn. Startup marks persisted acquired runs interrupted and signals affected roles. Local reconciliation expires leases after 660 seconds, repairs orphan readiness and wakes uncovered Goals; it uses records/time, without a process handle. See `store_session.py:456`, `store_message.py:324`, `store_session_log.py:390`, `store_dispatch_readiness.py:59`, `agents.py:125`. |
| Frozen epochs, retained failures and joint verification | SessionLog/cursors provide ordered provenance and rebuildable readiness, but source ownership/integration is still a workflow responsibility: workspace construction and provider invocation do not allocate Git branches, constrain editable source or verify a combined snapshot. Use one integration owner and named file owners, with existing messages/files carrying immutable source/test hashes and unresolved failures. |

A Program evidence is its [current coordination plan](../../a-program/doc/2026-10-03-PARALLEL-CORE-BACKEND-PERFORMANCE-PLAN.md)
and `notify_core.cjs`, `watch_core.cjs`, `test_notify.cjs`, read without controlling
workers or reading session logs/secrets. The plan records separate worktrees,
prototype boundaries, exact epoch review, retained failures and combined-owner
tests; these are reported operational evidence, not compiler results rerun here.
It records early notification interruption of an active Wait, including 294.2237
seconds, while host/process recovery and a full six-hour elapsed wake remain
unverified. The relay checks regular newline-terminated files up to 8 KiB,
deduplicates file/hash pairs in memory and retains files on delivery failure.
Its acknowledgement proves notification acceptance, not Core review or approval;
its memory does not establish durable exactly-once delivery across restart.

Official documentation fetched on 2026-10-03 describes Codex Goals as persisted
thread-scoped objectives with lifecycle/budget accounting. [Using Goals in Codex](https://developers.openai.com/cookbook/examples/codex/using_goals_in_codex).
App Server documents `thread/resume`, persisted `thread/goal/*`, guarded
`turn/steer` and `turn/completed` terminal statuses. A terminal turn is evidence
about that turn, not fulfillment of project requirements. [Codex App Server](https://learn.chatgpt.com/docs/app-server).
This establishes an integration surface; it does not prove that AIze's current
`codex exec` subprocess uses it or that another provider has the same lifecycle.

### Assessment

Agent conclusion: AIze already has the useful separation between durable Goal
facts, role dispatch and reviewer authority. Preserve SessionLog/cursors as the
work stream and readiness as a projection; avoid a second coordination graph.
The local DAG/liveness work improves this fit, but scheduling coverage is weaker
than verified process liveness or safe completion. A Core-equivalent GoalManager
needs explicit evidence review and waiting semantics, not another worker request
whenever it cannot yet complete. Recommendations below remain agent proposals.

Core operational instructions implement the human's publication request: this
session changes only its audit document, verifies preservation and sends Core
the exact file hash/source evidence. Core reviews and commits/pushes only that
document, excluding unrelated dirty files; this session does not stage or push.
The publication request supersedes the brief's prohibition only for this audit.
Core permits this audit Goal to finish after verified handoff; publication remains
Core's responsibility. No implementation, live-state or other-doc changes and no
repair work are added to this assignment.

### Plan

- [x] Inspect repository instructions, current source, tests and relevant design notes.
- [x] Pin observations to the revision and hashes of relevant local edits.
- [x] Trace goal/session ownership, dispatch, completion, cancellation and recovery.
- [x] Map observed multi-worker practices to supported, missing or conflicting paths.
- [x] Record findings with code references, counterexamples and evidence limits.

## 2. Stability and Next Steps

### Subjective (User)

2026-10-03, English paraphrase: provide useful knowledge for aize's stability from
the current development workflow, without making it part of A Program development.

### Objective (Code)

Fresh isolated verification used private copies under
`/tmp/aize-goal-audit-20261003/`; no user runtime/database was opened. Existing
tests disable external providers or use fake executables/mocks. No installation
or live service was needed.

| Verification | Exact scope and result |
| --- | --- |
| `python3 -m unittest discover -s tests -v` | Current frozen worktree: **77/77 pass**, exit 0, 28.811 s. Separate HEAD-only copy: **69/69 pass**, exit 0, 32.748 s. `PYTHONDONTWRITEBYTECODE=1`, `AIZE_ENABLE_EXTERNAL_AGENTS=false`, private `TMPDIR`, `PYTHONPATH=src`. Logs: `unittest.log`, `unittest-head.log`. |
| `python3 probe.py snapshot/src` / `python3 probe.py head/src` | Temporary assertion probes: **9 current / 7 HEAD pass**, both exit 0. Six counterexamples/behavior checks and declaration guards agree on both revisions; two additional current-only controls verify two-parent reopening/review and simulated expired-lease recovery with gap_count 0. |
| `node test_notify.cjs` | Private copies of the existing A Program helpers: **5 mock controls pass**, exit 0 (absent/active/idle/stale/relay); private Unix sockets, no real Core connection. |

The suite covers concurrent message producers, append ordering, torn-log-tail
recovery, lost UserInput metadata reconciliation, role windows/cursors,
follow-up coalescing, protocol failures, scheduling and daemon Lots. It does not
cover the completion and whole-state mutation interleavings below; passing the
suite therefore does not refute those probes.

| Finding | Reproducer and observed result | Source boundary |
| --- | --- | --- |
| **F1: stale completion can suppress newer required work** | During a GoalManager turn, declare complete, then append UserInput before the runner returns: input persists, Goal becomes complete, no ready entry and the next dispatch returns None. Updating the Goal body during that turn likewise permits the old run to declare/complete the replacement requirement under the same Goal ID. A separate recovery interleaving interrupts a declared run and acquires a replacement; returning the old result still marks the Goal complete and old lease released while the replacement remains acquired. | Declaration guards validate ownership only when declared (`store_session.py:1083`). Commit checks existence, not acquired ownership or the observed requirement/log window (`store_dispatch.py:235`); completing resolves all live readiness for that Goal (`store_session.py:1017`). Worker/error commit paths also lack a lease-state fence (`store_dispatch.py:499,584`), though these probes exercise GoalManager commits. |
| **F2: an unlocked mutation can erase in-flight ownership** | Pause `set_session_active` after its load; a second Store acquires and saves a real dispatch lease under the state lock; finish deactivation's stale save. The acquired run is deleted. This is a deterministic interleaving of real Store operations, not an observed production incident. | `store_session.py:456` lacks the transaction lock; `update_goal` is also unlocked (`:973`). Storage saves complete collections and removes items absent from the saved snapshot (`store_files.py:112,299`). Per-file atomic rename does not serialize the whole read/change/save transaction (`:672`). |
| **F3: AIze's resume token does not resume a provider thread** | Mock two external Codex calls with the same token: both invoke fresh `codex exec`; neither issues resume. The token is merely prepended to the prompt and returned unchanged. Durable AIze transcripts exist, but this cannot establish a persistent provider Goal/context. | AIze generates `thread-agent-*` itself (`store_dispatch.py:825`); `_run_external` uses prompt text and does not capture a provider thread ID (`agents.py:99`). `test_cli.py:2980` asserts that prefix, rather than provider resumption. Classified as a verified integration gap, not a provider crash. |

All three findings reproduce on HEAD and the edited worktree; no causal blame
is assigned to the local changes. Positive controls reject Worker completion and
duplicate declarations. The local additions also pass orphan/lost-work recovery,
timeout conversion and starvation tests, plus the two added temporary controls.

Two bounded design gaps are separately verified: an incomplete waiting decision
makes WorkerAgent ready; `update_goal` overwrites body/author while GoalStateChanged
records state/reason without the prior body. The probe finds no original body in
SessionLog, though an older dispatch transcript can retain it when such a turn
exists. Durable requirements should not depend on having previously dispatched.

Reproduction command, from the evidence directory: set
`PYTHONDONTWRITEBYTECODE=1 AIZE_ENABLE_EXTERNAL_AGENTS=false`, then run
`python3 probe.py snapshot/src` (or `head/src`). The temporary script mocks the
runner and controls operation order; it never edits implementation. Probe/results
SHA256s are respectively `5072f70eaf23a9ac219a3762dfc9f2940c165259ea01d1c18e6ac8daab7057f9`,
`9bc6580cbc19c88be7ff96a6691fd98fd4fee4805bcf6197c972ff53c9a472f2`
and `d9c898cab26569ac47e69acdef1ee0e4005cd3a5b92699bef0f65fc085860a94`.

The 27-file source/test/launcher/README/config manifest is
`tested-source.sha256`, SHA256
`554dd9389e8ddd9bc628dac63ae7ef3cf84161de5bea3c6a0a2522d879950d14`.
Relevant local-edit SHA256s (all 18 dirty tracked files plus the existing note):

```text
9a7d31395504e573dc496b2528865d87e6e7ed534d67cf76b4a5368a0aa1c6a3  README.md
50be6cd3272f70eede7a629b10ff32bdf81ffa6829c4c348715820abcdce3c34  src/agent_api.py
58109b0e3baa0e1a3b042a17e942950d893ad2ff4b2d7b3fa472caba4ac47c21  src/agents.py
da468a7ce8d26a2369afed78c7b237bafdf78a818e1d3b790f3517004da62e0f  src/cli.py
88eccea04de7574696c38d32c620ddb44e929e49c42e7ed9a261670b8cb5f87e  src/cli_console.py
d012767aaa39544d501e64d55896a3d84cf327f2d729c659a73deae3ff92d43e  src/cli_render.py
d854202958c37347a3b2ad140cb0c62ca591e27698881427418a6310022c5f1c  src/cli_workers.py
1c5e9768e2dadcb31f5ea79fb394d5fb9f2b260069d5cab0c57b72b547bdfe1e  src/dispatch_policy.py
80b2f17c61e669c48c49a072710e3cb1dffa8f295a07e81f95d9c4757ca5c45f  src/dispatch_projection.py
9b65189f47954a9df28f79af73ed441bd3f6f25ed257489c030f71e2f7e2e4c3  src/store_defs.py
2e2c166c02b0269a94cd3739b6765fac0eed564e6f1a3aca695a9ffc0e587e82  src/store_dispatch.py
b6a2949f21727279ebeb90d2d0b03b8a5235d1a9f5203ceabf7fcd5d2b261eba  src/store_dispatch_readiness.py
83d8eb2af19973966d8b67e8bf6855043c1bef34ae644ff9820361a91fcc1774  src/store_prompts.py
ccd40cd5a9efef48fcfc5fce8e90ea63ead54584a26f7fbdfe6d56db0bd071f3  src/store_query.py
a45deeb5b103087c9d53fdf7e1cfa906e465a35f670aef23b62351a81fccc1ab  src/store_session.py
954b1d162a2f5a63c1590c027fc90ecb78cbd8c518ca7c59d1e90587496f9f90  src/store_session_log.py
5b0f88474c025c24aacbde61cf81a01f3d7d9922ab983f9986ea56460ff380ec  tests/test_cli.py
d8fa226e510c703250882613dcea8983c26d4ebe770e5d2cfce94338d01266f9  tests/test_dispatch_policy.py
caf4e8bd1f0731513b32faf2e43235da01a2f3272f51229a793b5d80ee2b0b19  doc/2026-08-10-dispatch-liveness-invariant.md
```

A Program plan snapshot SHA256:
`82c22c175db54cf5d07414d18dd11cb04832a7806483af5350cbc1c6cff72973`.
The three helper hashes are in `a-program-evidence.sha256` (SHA256
`808ec93620ce8f9c3634be03294734cc27e03c67396b9775673cf6c64b2ccf2c`).

Limits: no actual provider execution/resume, provider Goal lifecycle, host crash,
process-tree cancellation, power-loss injection, remote delivery, full six-hour
wake, real source integration or performance measurement was verified. Existing
tests model selected recovery failures, not every multi-file crash cut. No cause
for the human's live stability experience is inferred. Temporary evidence is
reviewable locally but is not a permanent service or archival guarantee.

### Assessment

Agent proposals, in the smallest useful follow-up sequence; none is implemented
or approved by this audit:

1. **P1 — serialize existing state mutations.** Reuse `_state_lock` across each
   read/change/save transaction, including goal edits and activation. Turn F2
   into a deterministic concurrency regression: a previously acquired run and
   unrelated producer metadata must survive the competing mutation.
2. **P1 — fence completion and late results.** At declaration and final commit,
   validate the current acquired run and unchanged requirement/log context.
   Retain stale output as evidence without completing or consuming newer work.
   Record dated Goal body revisions in SessionLog; test all F1 cases, newer child
   events, duplicate/late reports and error returns against a replacement run.
   Preserve the latest correction and require a fresh review on mismatch.
3. **P2 — make provider identity real before promising provider Goals.** Keep
   AIze's Goal authoritative; choose an explicit provider continuation contract,
   persist returned thread/turn IDs on the existing role thread and verify resume
   after process restart. Test interrupted/failed/completed turns separately from
   Goal completion. Do not infer native Goals from a provider name or prompt.
4. **P2 — support bounded waiting and qualify multi-session integration.** A
   GoalManager waiting on a named child/event/deadline must remain incomplete
   without requesting redundant implementation. Derive wake readiness from the
   existing log; verify event-or-timer resume and user pause/cancel semantics.
   Then exercise one coordinator plus two owned source lanes: frozen hashes,
   retained failure, correction while in flight, restart and combined review.
   Branch/worktree policy and source/test evidence belong in existing task briefs
   and messages/files, with one integration owner; no new graph/cache authority.

Defer general orchestration infrastructure and performance tuning until these
ownership/completion boundaries are verified. The current positive controls
support retaining the DAG, role permissions and log/projection separation.

### Plan

- [x] Inspect relevant existing test coverage; run isolated tests only when safe.
- [x] Distinguish verified defects, design gaps and unverified hypotheses.
- [x] Propose prioritized, testable follow-up work without extra state authorities.
- [x] Verify final source/doc preservation and prepare Core's exact hashed outbox handoff.
- Completion (Core operational criterion): Core can review the audit against the inspected code; no code,
  live state, other project or unrelated document is changed. Deliver the exact
  audit hash and final source verification to Core; this audit Goal ends after
  verified handoff. Core alone reviews, commits and pushes only this document
  without including unrelated dirty files. Recommendations remain proposals.

Final preservation check, 2026-10-03: all 68 snapshotted files (67 non-audit
tracked files plus the existing untracked liveness note) match their initial SHA256s; HEAD and
the complete Git status match the initial inspection. Implementation, prior local
edits and other documents are unchanged. No live state was used or changed, and
this session made no commit, staging operation or push. Core's report is
`/tmp/aize-goal-audit-20261003/outbox/ready.txt`; its exact document hash is the
worker review boundary. Core handles audit-only publication.

Core review, 2026-10-03: the original handoff document hash
`bc2e669e68c5f447401f9d8f6499ffbf9765b34a14f0d610162326218eef83d4`
and all seven source/result manifest hashes match. Core independently reran the
nine current and seven HEAD probes, all exit 0, inspected the F1/F2/F3 source
paths and verified preservation of all 68 non-audit files. Unit-suite results
above remain worker verification, not a second Core suite run. Core removed a
mislabelled Subjective note: provenance/staging procedures are Core operational
instructions, not a human statement. Only this audit document is selected for
publication; recommendations remain unimplemented.
