# Restart Debug And Detach Fixes

## Summary

This note records the restart-related fixes made while stabilizing the Aize HTTP mesh.

## What Was Changed

- Moved restart/debug references from `./temp/...` to `./.temp/...`.
- Fixed `.temp/restart-debug/probe_restart.py` so it reads and writes under `.temp/restart-debug/` instead of the old `temp/` path.
- Updated `.temp/restart-debug/AGENTS.md` so the documented commands match the actual restart layout.
- Hardened `restart_codex_http_mesh.sh` so the newly launched parent process is started with:
  - `nohup`
  - `setsid` when available
  - stdin detached from the caller

## Why This Was Needed

There were two different problems mixed together:

1. The restart diagnostics were partly stale after `temp/ -> .temp/`.
   - This made restart probing and operator guidance point at the wrong files.
   - The result was false failure signals and confusing reports.

2. The parent relaunch path needed stronger detachment.
   - The supervisor was already detached correctly.
   - The newly launched parent process was only using `nohup`.
   - To reduce the chance of the child inheriting an unwanted session/terminal relationship, the launch path was updated to use `setsid` as well.

## What Was Difficult

- The observed failure did not cleanly match the supervisor logs.
  - The logs often showed successful shutdown, relaunch, and health recovery.
  - Meanwhile the operator still experienced restart failure and manual intervention.
- This meant the problem was not just "the process did not come back".
  - Part of the issue was the measurement path itself.
  - Part of the remaining risk was in how the relaunched parent detached from the restart context.
- The restart flow is also awkward to debug because a successful restart deliberately destroys the old process tree that is being inspected.

## Current Result

- The detached restart path now matches the documented `.temp` layout.
- The new parent launch path is more aggressively detached.
- A direct restart test through `./.temp/restart_codex_http_mesh.sh` completed with `4123/health` returning `ok: true`.

## Remaining Risk

- The restart path should still be exercised more than once in a row to confirm there is no intermittent race.
- The probe logic was improved, but restart diagnostics are still sensitive to timing because old and new process trees can overlap briefly.
