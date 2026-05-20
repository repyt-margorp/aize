Implemented a narrow panic-recovery fix for Codex capacity failures in Entrance-style sessions.

Behavior changed:
- Codex errors containing `at capacity` now follow the same transient-failure path as rate-limit style failures.
- The runtime schedules auto-resume with a 15 minute retry window instead of treating the failure as a hard-stop-only condition.

Files touched:
- `src/runtime/agent_service.py`
- `tests/test_service_control.py`

Verification:
- `python3 -m unittest tests.test_service_control -q`
- Live session evidence checked in `.aize-state/sessions/repyt/8149fee8e6aeac43/timeline.jsonl` and `.aize-runtime/logs/service-codex-user-response-request-ui-001.jsonl`

Remaining risk:
- This only covers the current Codex `at capacity` wording. Different transient-capacity strings from providers may still need to be normalized separately later.
