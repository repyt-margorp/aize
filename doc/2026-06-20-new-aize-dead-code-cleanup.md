# New AIze Dead Code Cleanup

## User-visible behavior

- No CLI behavior changed.
- The README now reflects the current MessageLog boundary:
  - Session capabilities are Session metadata, not Messages.
  - Agent stdout/stderr is dispatch-run step output, not Messages.
  - Agent API calls create explicit AIZE Messages.
  - Console startup can enqueue background dispatch for queued Active/Incomplete SessionGoals.

## Code cleanup

- Removed unused `Store.create_goal`; `Store.update_goal` is the single SessionGoal write path.
- Removed unused `Store._message_from_session`.
- Removed unused `Store._render_dispatch_result`.
- Removed generated `__pycache__` directories under `src` and `tests`.
- Removed stale untracked `docs/` notes that contradicted the current `doc/` implementation logs and README.

## Deferred cleanup candidates

- Non-interactive generic `send` / `recv` still exist for low-level message testing and scripting. They should only be removed after deciding whether the CLI should expose raw IPC.
- Legacy state migration helpers still exist because live `.aize-state` may contain records from earlier same-day schema iterations.
- Remote AIZE handoff is still represented as local durable messages; the transport is not implemented yet, but the provider path is covered by tests.

## Verification

```bash
python3 -m py_compile src/*.py
python3 -m unittest discover -s tests -q
```

Result: 21 tests passed.
