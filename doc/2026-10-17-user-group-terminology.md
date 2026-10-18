# User/Group Terminology Unification

Date: 2026-10-17
Status: accepted design; phased implementation. Normative anchors: POSIX/Unix
user management and modern IAM (Kubernetes-style RBAC) practice. MINIX remains
the structural anchor (per-process privilege grants), not the vocabulary
authority.

## Problem

The runtime carries domain terms that match neither Unix, MINIX, nor modern
user-management vocabulary:

- `account` collection and `account:{username}` address-space words for what
  every anchor calls **user/user identity**.
- `roles: ["root","admin"]` on user records: a disconnected label list with no
  permission effect, while Unix calls membership lists **groups**.
- Agent job-class names used as both "who does work" and "permission set name"
  while the dispatch engine means **slots** (GoalManager/WorkerAgent).

## Decisions

1. **Ownership of the word.** Atomic per-message allow/deny checks keep the
   MINIX/Linux-faithful name **capability** (they are process-like stamped on
   the Session record). Nothing above may swallow capabilities.
2. **Identity words become standard.** `users` is the collection name; a user
   record is an identity (username + login material), no authority by itself.
   Service agents will later join as non-login users (the `www-data` model).
3. **Membership words become standard.** `groups: [...]` replaces
   `roles: [...]` on user records. Phase 1 only renames; group semantics
   (spawn eligibility + dispatch priority) arrive with service identities.
4. **Job classes become groups by name only in phase 3.** The
   GoalManager/WorkerAgent distinction stays as the per-session slot contract
   (capability stamping + separation of duties). Until phase 3 the
   slot-facing names stay in prompts, envelope attributes, and
   `AIZE_AGENT_ROLE` for external contract stability.
5. **Protocol artifact names survive.** `account:{username}` endpoint family,
   `account-home` session id scheme, and `AIZE_AGENT_ROLE` are durable
   protocol strings (same policy as `remote-aize`); they are not renamed in
   phase 1.

## Phase 1 (this implementation)

- `kernel/model.py`: `Account` -> `User` dataclass; record field
  `roles` -> `groups`.
- `kernel/auth.py`: `_build_user`, `_public_user`, `create_user`,
  `user_home_session`, `users()`.
- Storage: the logical item-collection key `users` keeps the legacy on-disk
  directory `metadata/accounts/` through a generic directory-name alias in
  `SplitStateStorage` (collection_dir_names). No on-disk rename, no race with
  running daemons from older code.
- Dual-read record migration: load-time conversion `roles` -> `groups` inside
  defaults repair; saves thereafter write `groups` only. Old code reading
  `roles` degrades to an empty label list, never a crash.
- CLI: `users` (alias `accounts`), `create-user` (alias `create-account`),
  `--group` (legacy `--role` accepted), `account-home` resolution call renamed.
- Console wording untouched beyond existing commands.

## Later phases

2. Groups carry real semantics: membership checks at spawn eligibility and
   per-group dispatch priority for service identities. — **Implemented
   2026-10-17 (phase 2)**: `unit.spawn_groups` restricts which user groups may
   start a Unit's Sessions across every activation path (manual
   `create-session`/`start-goal`, `run-scheduled-units`, `run-startup-units`);
   scheduler runs isolate a gated Unit as a skip record instead of failing the
   batch, `set-unit-spawn-groups` edits the list, and `groups` lists each
   group with its members. Per-group dispatch priority for service identities
   stays reserved for phase 3.

3. Service identities — **implemented 2026-10-18 (phase 3)**, per the
   "Agent service identities" section of
   `doc/2026-10-17-runtime-layer-boundaries.md`, reconciled with this note:
   - **Non-login `svc-*` users.** Service accounts are user records with
     `status: "service"` (never authenticate), no password material, and a
     `provider` execution profile. The runtime auto-registers one account per
     provider entity (`svc-codex`, `svc-claude`, `svc-remote-aize`). The
     `svc-` namespace is reserved: login users cannot take `svc-*` names and
     service users must. Service users get no home Unit/Session.
   - **Groups carry appointment eligibility.** Job classes gain group names:
     `manage` (GoalManager slot) and `work` (WorkerAgent slot). Role tokens
     GoalManager/WorkerAgent stay as durable protocol names (capability
     stamping, SessionLog targets, prompts); the group names are the
     appointment vocabulary, and remote providers are never manage-capable.
   - **Per-role priority lists.** `agent_profiles[role]` carries an ordered
     `accounts` list edited by `set-agent` (single provider → its account) and
     by the new `set-agent-priority`. Dispatch appoints the first eligible
     account (exists, active-or-service, slot group member) whose provider is
     available; an unavailable provider falls through the list. An empty list
     keeps the protocol identity with the profile's provider, so pre-phase-3
     behavior is reproduced exactly.
   - **Run lease = credentials of the moment.** A dispatched run records
     `agent_user` / `agent_group` / `agent_provider`; the appointed identity
     sends Messages as `user:{svc-name}` (`agent:{role}` stays accepted as the
     legacy alias, and `account:{name}` dual-reads as `user:{name}`); provenance
     of completion/waiting/schedule decisions binds actor to the leased
     identity (`declared_by`, runtime run binding).
   - **Thread keys.** Agent threads key per `(session, role, account)` —
     `session:Role` without an appointed account (legacy shape), else
     `session:Role:svc-name`; a legacy role-keyed thread is adopted by the
     appointed account of the provider that already owns its provider threads,
     so no provider continuation context is lost (F3 fix).
   - **Env contract alias window.** `AIZE_AGENT_GROUP` (`manage`/`work`) and
     `AIZE_AGENT_USER` are canonical; `AIZE_AGENT_ROLE` stays accepted and is
     still exported for compatibility.

   Implementation notes beyond the boundaries-doc commitments: dispatch
   readiness derivations match agent senders through a provenance vocabulary
   (Message payload `agent_role`/`agent_user`), falling back to the legacy
   `agent:{role}` endpoint comparison for dual-read; the implicit WorkerAgent
   request and run-message stamps carry the leased identity. Verification:
   `tests/test_service_identities.py` (19 probes: auto-registration, non-login
   gate, namespace reservation, profile priority lists, availability
   fall-through, appointment, thread adoption keys, `user:` endpoints, run
   binding, legacy alias window), full suite 123/123 OK with
   `scripts/check_layer_imports.py` clean.

Rules that outlive the phases (from
`doc/2026-10-17-runtime-layer-boundaries.md`): the L1 lock contract stays the
single writer path; every state-format change lands with dual-read tolerance
and a permanent regression test; the F-level probes (completion fence, stale
results, bounded waiting) must keep passing unchanged.
