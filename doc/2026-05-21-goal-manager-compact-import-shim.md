Behavior changed:
- GoalManager compaction no longer crashes runtime startup when a running process sees a `runtime.session_view` module that does not yet expose `session_agent_assignment_counts`.
- HTTPBridge and the CLI adapter now fall back to a local compatibility shim for agent-count summaries.

Files touched:
- `src/runtime/cli_service_adapter.py`
- `src/runtime/http_handler.py`

Verification run:
- `python3 -m py_compile src/runtime/session_view.py src/runtime/http_handler.py src/runtime/cli_service_adapter.py`

Remaining risk:
- This is a compatibility guard, not a root-cause rewrite of why one process observed an older `session_view` export set during compaction. If partial-update behavior recurs for other helpers, they may need the same import-hardening pattern.
