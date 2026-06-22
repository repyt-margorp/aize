# Unit automation

Units can now carry an optional `automation` command. When a scheduled Unit is
due, AIZE starts the Unit Session, records the initial prompt when present, runs
the configured command, stores its stdout/stderr on a system message, and marks
the SessionGoal complete when the command exits successfully.

This is intentionally generic: a Unit can launch a local app, run a health
check, poll an external process, or drive a watchdog script without adding
app-specific behavior to AIZE.

Interval schedules accept either `every_hours` or `every_seconds`. Second-level
intervals are useful for watchdog-style Units that should run frequently under
the foreground `aize daemon` loop.

Use `aize daemon --no-dispatch` when a process should only run due scheduled
Units and should not dispatch queued agent work. This keeps automation-only
watchdogs isolated from unrelated SessionGoal queues while preserving the
default daemon behavior for normal agent dispatch workers.

Example:

```bash
PYTHONPATH=src python3 -m cli create-unit browser-watchdog \
  --goal-text "Keep the browser workflow healthy." \
  --schedule-every-seconds 15 \
  --automation-command bash \
  --automation-command scripts/watchdog.sh \
  --automation-cwd ../browser-workflow
```

Enabled plugin launcher apps can be synced into Units with `sync-app-units`.
Launcher `schedule.interval.seconds` maps to the same second-level schedule.

## Verification

- `python3 -m unittest tests.test_cli.CliTests.test_scheduled_unit_supports_second_interval`
- `python3 -m unittest tests.test_cli.CliTests.test_scheduled_unit_runs_automation_command`
- `python3 -m unittest tests.test_cli.CliTests.test_sync_app_units_creates_launcher_unit_and_preserves_next_run`
- `python3 -m unittest tests.test_cli.CliTests.test_daemon_can_run_scheduled_units_without_dispatching`
