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

3. Service identities: non-login `svc-*` users, per-role priority lists,
   `(session, group)` thread keys, `agent:` endpoint names become
   `user:{svc-name}`, `AIZE_AGENT_ROLE` retires behind an alias window.

Rules that outlive the phases (from
`doc/2026-10-17-runtime-layer-boundaries.md`): the L1 lock contract stays the
single writer path; every state-format change lands with dual-read tolerance
and a permanent regression test; the F-level probes (completion fence, stale
results, bounded waiting) must keep passing unchanged.
