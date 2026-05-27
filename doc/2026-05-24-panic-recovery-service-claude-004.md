# Panic recovery — source session `1a33b8c596b9874c` (AIze System Monitor)

## Immediate cause

`service-claude-004`, acting as the GoalManager for the source session
`1a33b8c596b9874c`, was selected by `restart_resume_claim` at
`2026-05-24T12:10:17Z`. On the first turn the Claude provider returned
HTTP 429 with a synthetic assistant message
`"You've hit your org's monthly usage limit"` (rate_limit_event,
`rateLimitType: five_hour`, `overageStatus: rejected`,
`overageDisabledReason: org_level_disabled_until`).

The worker then exited with `RuntimeError('claude failed with exit code 1')`,
which set the per-service audit file
`services/service-claude-004.audit.json` to `audit_state: panic`
(`updated_at: 2026-05-24T12:10:24Z`). The matching service file
`services/service-claude-004.json` was left at `status: queued` with three
`pending_work_items` (one `restart_goal_review`, two `lifecycle_owner_lost`).

The source session's GoalManager state file (`goal_manager/state.json`)
had already been flipped to `audit_state: all_clear`,
`progress_state: in_progress` at `12:10:04Z`, but
`load_session_audit_summary` reported `panic` because the per-service
audit file was newer.

After that, every restart cycle re-fired
`agent_audit_panic_restart` for `service-claude-004` because
`load_session_audit_summary` continued returning `panic`. The current
recovery (this conversation, `4a6123037b67e820`) was created at
`2026-05-24T12:10:29Z` and was re-invoked on the run that started at
`2026-05-24T14:43:17Z` for the same reason.

## Smallest viable fix

Used the existing recovery helper:

```
python3 scripts/recover_panicked_session.py --username root --session-id 1a33b8c596b9874c
```

This produced a backup at
`.aize-state/sessions/root/1a33b8c596b9874c/.panic_recovery_backup.json`
and applied:

- `services/service-claude-004.audit.json`: `audit_state` `panic` → `all_clear`.
- `session.json`: `restart_resume_claim_run_id`,
  `restart_resume_claim_service_id`, `restart_resume_claimed_at` all cleared
  (they pinned the dead Claude binding).

No other audit files needed touching (all already `all_clear`), and
`goal_manager/state.json` was already non-`failed` / non-`panic`. No code
changes were required — the issue was purely a stale per-service panic
record combined with a stranded restart-resume claim that pointed at a
provider that had been externally rate-limited.

## Verification

- `load_session_audit_summary` for `root / 1a33b8c596b9874c` now returns
  `audit_state: all_clear` (`updated_at: 2026-05-24T14:47:23Z`).
- `session.json` shows `goal_active=true`, `goal_completed=false`,
  `goal_progress_state="in_progress"`, and the restart-resume claim fields
  are empty.
- The next restart pass will no longer fire
  `agent_audit_panic_restart` for `service-claude-004` (gate at
  `compaction.py:570` requires `goal_audit_state == "panic"`).
- The source session retains its `goal_manager.pending_work_items`
  (1 `restart_goal_review` and 2 `lifecycle_owner_lost`), so the
  GoalManager has concrete work to drive when next dispatched.

## Remaining risk

- The provider rate limit is an external condition. The source session's
  `goal_manager_priority` already lists `codex` first, so dispatch should
  rebind to a codex worker; if a future review re-targets `claude` while
  the org limit is still in force, the same panic will recur. Code in
  `panic_recovery._select_recovery_provider` already swaps providers on
  transport-like markers but `"You've hit your org's monthly usage limit"`
  is not currently in that marker list. That heuristic could be extended,
  but the immediate failure here is now cleared without code changes.
- A rollback is available via
  `python3 scripts/recover_panicked_session.py --username root --session-id 1a33b8c596b9874c --rollback`.
