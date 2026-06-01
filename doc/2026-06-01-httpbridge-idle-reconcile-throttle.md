# HTTPBridge Idle Reconcile Throttle

## User-visible behavior

HTTPBridge could become unresponsive while the Unit runtime process was still alive. The failure mode observed on 2026-06-01 was `service-http-001` consuming a full CPU core while `/health` timed out.

## Cause

`session_runtime_payload` reconciled Active/InProgress sessions that appeared idle while building session list payloads. Its duplicate guard only lived for a single payload build, so repeated UI polling could enqueue the same `active_in_progress_idle_reconcile` dispatch over and over when the selected worker service was temporarily unavailable.

HTTPBridge also ran several background tasks that scanned all sessions and sometimes full timelines. With many sessions, those scans contended on persistent-state locks and made the UI service spend CPU on maintenance work instead of request handling.

## Change

Added a process-local 120 second throttle per `username::session_id` around this adapter-side idle reconcile path. When a reconcile dispatch is accepted, the payload summary is also marked as goal-manager `queued` for that render.

Reduced HTTPBridge background scanning:

- overview cache warmer: disabled; overview is computed on demand through the TTL cache
- user-response wait watcher: disabled in HTTPBridge; this needs an indexed scheduler instead of HTTPBridge all-session scans
- auto-resume watcher: disabled in HTTPBridge; this needs an indexed scheduler instead of HTTPBridge all-session scans
- Unit schedule watcher: 60 seconds

Startup reconcile now stays metadata-first and no longer reads every active session's full timeline before deciding whether to continue.

Added a diagnostic thread dump hook for HTTPBridge. Sending `SIGUSR1` to the `service-http-001` process writes all Python thread stacks to `.aize-runtime/logs/service-http-001.thread-dump.log`.

## Verification

- `PYTHONPATH=./src python3 -m py_compile src/runtime/cli_service_adapter.py src/runtime/http_handler.py`
- `PYTHONPATH=./src python3 -m unittest tests.test_http_handler_goal_save -q`
- Confirmed `https://127.0.0.1:64123/health` returns 200 after runtime restart.

## Remaining risk

This is a runtime-pressure guard, not a full dispatch-queue redesign. Router-side deferred delivery and unavailable worker recovery should still be audited separately. Auto-resume and user-response wait handling also need an indexed scheduler before they can safely return as background jobs.
