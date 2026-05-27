# Panic recovery — source session `11f9b83a01a310ee` (AIze System Monitor)

## Immediate cause

`service-claude-001`, acting as GoalManager for the source session
`root/11f9b83a01a310ee` ("AIze System Monitor 2026-05-23 18:37 UTC"),
was selected for `restart_resume` at `2026-05-24T12:10:09Z`. On the
first turn the Claude provider returned HTTP 429 with the synthetic
assistant message `"You've hit your org's monthly usage limit"`
(`rate_limit_event`, `rateLimitType: five_hour`,
`overageStatus: rejected`,
`overageDisabledReason: org_level_disabled_until`,
`resetsAt: 1779633600` ≈ `2026-05-25T04:00Z`).

The worker exited with `RuntimeError('claude failed with exit code 1')`
at `12:10:17Z`. `services/service-claude-001.audit.json` flipped to
`audit_state: panic` (`updated_at: 2026-05-24T12:10:18Z`), and the
restart claim was pinned to the dead Claude binding via:

- `restart_resume_claim_run_id`:
  `system-restart-run-20260524-121001-11f9b83a01a310ee-agent-service-claude-001`
- `restart_resume_claim_service_id`: `service-claude-001`
- `restart_resume_claimed_at`: `2026-05-24T12:10:08Z`

The panic recovery flow created the recovery session
`b0b73ccf7c2518bb` at `12:10:21Z` (this conversation), which was
re-invoked on the run that started at `2026-05-24T14:43:26Z`.

The Claude error text (`"claude failed with exit code 1"`) does not
match `_is_usage_limit_error_text` markers, so the live
provider-fallback path in `agent_service` was never triggered — the
rate-limit body was emitted only on stdout (`claude.assistant.text`
and `result.api_error_status: 429`) while `proc.stderr` was empty,
so `providers/claude.py:run_claude` raised the generic exit-code
`RuntimeError`. Out of scope for this minimal recovery; recorded here
as the underlying root cause for follow-up.

## Smallest viable fix

Used the existing recovery helper:

```
python3 scripts/recover_panicked_session.py --username root --session-id 11f9b83a01a310ee
```

This wrote a backup at
`.aize-state/sessions/root/11f9b83a01a310ee/.panic_recovery_backup.json`
and applied:

- `services/service-claude-001.audit.json`: `audit_state` `panic` →
  `all_clear` (`updated_at: 2026-05-24T14:49:27Z`).
- `services/service-codex-008.audit.json`: stale `panic` from
  `2026-05-23T18:41:33Z` → `all_clear`.
- `session.json`: `restart_resume_claim_run_id`,
  `restart_resume_claim_service_id`, `restart_resume_claimed_at`
  all cleared.

`goal_manager/state.json` was already at
`state: queued`, `audit_state: all_clear`, `progress_state: in_progress`
with the one `lifecycle_owner_lost` pending work item that survives
the run — so nothing else needed to be touched.

## Verification

After the apply, the runtime picked the queued GoalManager work and
dispatched it to a non-Claude provider:

- `2026-05-24T14:50:33Z` — `agent.turn_started` on `service-codex-010`
  (Codex-backed GoalManager).
- `2026-05-24T14:51:10Z` onward — `item.completed` (command_execution),
  `item.completed` (file_change) confirming concrete work on the
  monitor-record update flagged in `goal_manager/state.json:summary`.
- `service-claude-001.audit.json` remains `all_clear` (`14:51:53Z`),
  showing no fresh Claude rebind / panic.

The source session has resumed with new turns as required.

## Files touched / verification

- `.aize-state/sessions/root/11f9b83a01a310ee/services/service-claude-001.audit.json`
  (panic → all_clear, via recovery script)
- `.aize-state/sessions/root/11f9b83a01a310ee/services/service-codex-008.audit.json`
  (stale panic → all_clear, via recovery script)
- `.aize-state/sessions/root/11f9b83a01a310ee/session.json`
  (`restart_resume_claim_*` cleared, via recovery script)
- `.aize-state/sessions/root/11f9b83a01a310ee/.panic_recovery_backup.json`
  (rollback snapshot written by the recovery script).
- `doc/2026-05-24-panic-recovery-service-claude-001.md` (this log).

Verification consisted of reading the source session's
`runtime_journal.jsonl` tail (`agent.turn_started` → repeated
`item.completed` on `service-codex-010`) and re-reading the
post-recovery audit files.

## Remaining risk

- Claude org rate limit is still active until `2026-05-25T04:00Z`. Any
  fresh Claude binding before then will repeat the same exit-code-1
  panic path. The wider follow-up is to surface the rate-limit text
  through `providers/claude.py:run_claude` so
  `_is_usage_limit_error_text` and `_transport_like_panic` can catch
  it and trigger provider fallback instead of a recovery session. Not
  done here to keep this recovery narrow.
- The lone `pending_work_items[0]` (`lifecycle_owner_lost`,
  `source_service_id: service-codex-006`) is now being consumed by
  `service-codex-010`; if that consumer also fails before the goal
  resolves, a new recovery session will be created — but with the
  panic state cleared, the runtime will not re-fire
  `agent_audit_panic_restart` for `service-claude-001`.
