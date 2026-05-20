# Restart Recovery Module Split

## Behavior

- No runtime behavior is intended to change in this split.
- Restart/resume helper logic moved out of `src/runtime/compaction.py` into `src/runtime/restart_recovery.py`.
- `compaction.py` now keeps the orchestration side: scanning sessions, writing pending inputs, claiming restart work, and dispatching through the router.
- `restart_recovery.py` owns the smaller restart lifecycle predicates: restart claim ids, startup budget parsing, actionable pending detection, unfinished turn detection, dangling/terminal GoalManager history checks, latest review lookup, review cursor lookup, and timestamp age checks.

## Motivation

- `compaction.py` had grown into a mixed compaction, restart recovery, dispatch lifecycle, and GoalManager review module.
- The Active/InProgress dispatch invariant is easier to keep correct when the restart lifecycle predicates are testable outside the router/queue orchestration path.

## Verification

- `python3 -m py_compile src/runtime/compaction.py src/runtime/restart_recovery.py`
- `python3 -m unittest tests.test_goal_manager_compact -q`
- `python3 -m unittest tests.test_goal_manager_compact tests.test_entrance_page tests.test_session_template -q`
