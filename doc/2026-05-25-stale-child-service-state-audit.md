Changed user-visible behavior:
- Reconciled stale finished-session service records under `repyt` from leftover `running`/`queued` markers to terminal `complete`/`idle` state.
- Reconciled stale runtime registry/lifecycle records for `service-codex-004` and `service-claude-006` after confirming their OS PIDs were defunct.

Files touched:
- `.aize-runtime/state/services.json`
- `.aize-runtime/state/processes.json`
- `.aize-state/sessions/repyt/b03668468de85897/services/service-codex-002.json`
- `.aize-state/sessions/repyt/b03668468de85897/services/service-codex-003.json`
- `.aize-state/sessions/repyt/937de6c24387edc1/services/service-codex-003.json`
- `.aize-state/sessions/repyt/937de6c24387edc1/services/service-codex-development-lineage-check-001.json`
- `.aize-state/sessions/repyt/937de6c24387edc1/services/service-codex-010.json`
- `.aize-state/sessions/repyt/7cfc300118ceb328/services/service-codex-002.json`

Verification run:
- Checked session-level service files under `.aize-state/sessions/repyt/*/services/*.json` for `running`/`queued` records on completed finite child sessions.
- Checked live process table with `ps` for `service-codex-003`, `service-codex-004`, and `service-claude-006`.
- Confirmed `service-codex-003` remained live, while `service-codex-004` and `service-claude-006` mapped to defunct OS PIDs and were stale.

Remaining risk:
- The runtime does not currently auto-demote defunct worker processes in the registry, so a later code fix should make registry/lifecycle reconciliation automatic instead of state-only cleanup.
