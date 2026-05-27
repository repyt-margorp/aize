Behavior changed: restart recovery now proactively purges any stale `lifecycle_owner_lost` work item whose reason starts with `released_nonrunnable_session_service:` from a continuous communication session's `goal_manager/state.json` `pending_work_items` and the GoalManager pending input log, regardless of whether the ephemeral worker is being released again on this restart.

Background: the 2026-05-25 guard inside `enqueue_goal_manager_lifecycle_review` short-circuits and purges when `release_nonrunnable_session_services` re-fires for a continuous communication session. However, once the ephemeral worker has been released previously, `talk["service_id"]` is unset and the release chain no longer fires for that session — so the original guard cannot clear pre-guard entries that were already wedged in `pending_work_items`. The Entrance session carried such a stuck entry from `2026-05-24T17:43:03Z`. This change makes restart recovery itself perform the same purge once per restart per continuous communication session.

Files touched:
- `src/runtime/session_lifecycle.py` — renamed the internal helper to a public `purge_continuous_communication_restart_owner_lost_state`, making `state_path`/`goal_manager_state` optional and adding a return value that reports whether anything was removed.
- `src/runtime/compaction.py` — `maybe_resume_after_restart` now calls the purge for every continuous communication session it iterates over, then reloads `goal_manager_pending_inputs` so any downstream review logic sees the cleaned state.
- `tests/test_goal_manager_compact.py` — added `test_maybe_resume_after_restart_purges_stale_continuous_communication_owner_lost` to verify the stuck pre-guard entry is removed from both stores during restart recovery, no router dispatch is produced, and the existing skip path still holds for the idle continuous communication case.

Verification:
- `python3 -m py_compile src/runtime/session_lifecycle.py src/runtime/compaction.py tests/test_goal_manager_compact.py`
- `python3 -m unittest tests.test_goal_manager_compact tests.test_session_listing tests.test_http_handler_goal_save tests.test_entrance_page` — 301 tests OK.

Remaining risk: this purge only runs through the restart-recovery path (`maybe_resume_after_restart`). Live runtime verification still depends on a restart that picks up the new code. The first restart with this code will clear the existing Entrance stuck entry on the iteration where the session is observed as a continuous communication session.

Live verification 2026-05-24T19:40:05Z restart: confirmed cleared. Entrance session `goal_manager/state.json` now shows `pending_work_items: []` with `updated_at: 2026-05-24T19:40:04Z`, matching the restart timestamp. The wedged `lifecycle_owner_lost` entry from 2026-05-24T17:43:03Z is gone.
