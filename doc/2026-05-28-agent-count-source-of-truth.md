# Agent Count Source Of Truth

## Operational invariant

HttpBridge must keep provider-level assignment counts and per-session assignment badges derived from the same live runtime meaning:

- `Replying` means a currently running user-facing worker for that session.
- `Reviewing` means a currently running or queued GoalManager worker for that session.
- Historical `welcomed_agents` or `agent_contacts` are audit/contact history only. They must not count as currently assigned agents.
- Idle or waiting sessions must not display assigned agents merely because an agent participated earlier.

When changing SessionMap, SessionView, GoalManager runtime state, service leasing, or restart recovery, verify that provider header counts and the visible session-card badges agree.

## Root cause fixed

The SessionMap card counts and provider header counts both consulted session summaries, but the counting helpers treated historical joined agents as live assignments. That left old Entrance and recovery sessions showing agents while their runtime state was idle or waiting, and made the provider header totals disagree with the visible cards.

## Files touched

- `src/runtime/session_view.py`
- `tests/test_session_listing.py`

## Verification

```bash
PYTHONPATH=./src python3 -m unittest tests.test_session_listing tests.test_agent_priority
```
