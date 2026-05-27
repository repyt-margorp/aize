Behavior changed:
- The Codex provider wrapper now sends prompts over stdin for both fresh and resumed `codex exec` invocations instead of placing the full prompt on the process command line.
- This avoids `OSError(7, 'Argument list too long')` when large AIze session envelopes or history-heavy prompts are dispatched to Codex.

Files touched:
- `src/runtime/providers/codex.py`

Verification run:
- `codex exec --help`
- `codex exec resume --help`
- runtime recovery verification against parent session `repyt/0ac1231110d2881f` after restart, confirming a new worker turn completed successfully instead of panicking on launch

Remaining risk:
- Other provider wrappers still pass prompts by argv if they do so today; this fix is intentionally limited to the Codex path that caused the active panic.
