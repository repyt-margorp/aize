Behavior changed:
- SessionDAG provider status now reports `busy` from live assigned provider slots instead of only active reply/review turns.
- The provider pills still show replying and reviewing turn counts separately, so runtime activity visibility is preserved while busy reflects real assignment state.

Files touched:
- `src/runtime/session_view.py`
- `src/runtime/html_renderer.py`

Verification run:
- `python3 -m py_compile src/runtime/session_view.py src/runtime/html_renderer.py`
- `PYTHONPATH=./src python3 - <<'PY' ... build_worker_count_summary(...) ... PY`
  Confirmed `assigned_slots` counts leased Codex/Claude slots even when only one session is actively replying.
- Runtime UI verification attempted on the primary URL `https://127.0.0.1:4123/health` and an isolated runtime on port `4231`, but both were unreachable during this task.

Remaining risk:
- Browser-level verification is still pending until a reachable HttpBridge runtime is available. The helper and renderer paths are patched consistently, but the visual confirmation step could not be completed against a live page in this session.
