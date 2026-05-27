# System Monitor Scan Read-Only Speedup

- User-visible behavior changed: `runtime.system_monitor` now scans session timeline and pending-input files through read-only JSONL reads instead of hydrating full history text under repeated exclusive state locks.
- This keeps the report shape the same while avoiding the runtime stall where the scanner timed out before emitting JSON on a live workspace.
- Files touched: `src/runtime/system_monitor.py`.
- Verification run:
  - `PYTHONPATH=./src python3 -m unittest tests.test_system_monitor -v`
  - `time timeout 20s python3 -m runtime.system_monitor --runtime-root ./.aize-runtime --json >/tmp/aize-monitor.json`
  - `PYTHONPATH=./src python3 - <<'PY' ... scan_system_sessions(Path('./.aize-runtime')) ... PY`
- Result from the live runtime after the change: the monitor completed in about `1.14s` for `766` sessions and produced counts `findings=39`, `unresolved_user_input=1`, `unfinished_goals=13`, `stalled=26`, `system_problems=0`.
- Remaining risk: the monitor still reads every session timeline JSONL file on each pass, so very large history growth can still increase scan time even though the lock contention/hydration bottleneck is removed.
