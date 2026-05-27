# Session Skill Durable File Preserve

- User-visible behavior changed: session-skill file sync now preserves existing durable append-only files instead of overwriting them with the template content on a later skill normalization or refresh.
- This keeps records such as `monitor-record.md` intact across compaction, restart recovery, and any code path that replays the session skill manifest.
- Files touched: `src/runtime/persistent_state_pkg/_core.py`, `tests/test_session_skills.py`.
- Verification run:
  - `PYTHONPATH=./src python3 -m unittest tests.test_session_skills -v`
- Remaining risk: preservation is inferred from either `preserve_existing=true`, a description containing `durable`, or file content containing `Append one entry per scheduled run`, so other append-only templates still need to opt in explicitly if they do not match those signals.
