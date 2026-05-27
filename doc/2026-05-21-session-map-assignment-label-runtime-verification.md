## Behavior verified

- Verified the SessionMap assignment-count label change against an isolated HttpBridge runtime at `./.temp/session-map-label-verify2` on `http://127.0.0.1:44124`.
- Confirmed the server-rendered SessionMap HTML includes the new labels:
  - `Agents 0`
  - `GM Reviewers 1`
- Confirmed refreshed overview data for the same session still carries the split counts used by the SessionMap refresh path:
  - `"assigned_agent_count": 0`
  - `"goal_manager_reviewer_count": 1`

## Runtime setup

- Started a separate runtime without touching the active AIze runtime:
  - `PYTHONPATH=./src AIZE_RUNTIME_ROOT=.temp/session-map-label-verify2 AIZE_HTTP_PORT=44124 AIZE_HTTP_HOST=127.0.0.1 AIZE_TLS=false AIZE_CODEX_POOL_SIZE=2 AIZE_CLAUDE_POOL_SIZE=0 AIZE_GEMINI_POOL_SIZE=0 python3 -m cli.run_aize_unit --runtime-root .temp/session-map-label-verify2`
- Bootstrapped `root`, selected SessionMap parent `204d5583dad49e8a`, and seeded child session `0cf918d7bead034c` with:
  - one GoalManager reviewer on `service-codex-002`
  - one bound worker on `service-codex-001`
  - active goal state and running GoalManager runtime state

## Evidence

- Initial SessionMap HTML from `GET /?session_id=204d5583dad49e8a&scope=all` contained this rendered card fragment for `Worker Child`:

```html
<div class='goal-session-title'>Worker Child</div><div class='goal-session-agent-counts'><span class='goal-session-badge' title='Non-GoalManager agents currently assigned to this session'>Agents 0</span><span class='goal-session-badge is-on' title='GoalManager reviewers currently assigned to this session'>GM Reviewers 1</span></div></div><div class='goal-session-state'><span class='goal-session-badge is-on'>Goal Active</span><span class='goal-session-badge'>Goal In Progress</span><span class='goal-session-badge is-running'>Executing</span><span class='goal-session-badge is-audit-ok'>All Clear</span></div><div class='goal-session-meta'>root · 0cf918d7bead034c | from root via 204d5583dad49e8a</div>
```

- Refreshed overview data from `GET /overview?scope=all&session_id=204d5583dad49e8a` returned:

```json
{
  "session_id": "0cf918d7bead034c",
  "label": "Worker Child",
  "goal_manager_reviewer_count": 1,
  "assigned_agent_count": 0,
  "goal_active": true,
  "goal_progress_state": "in_progress",
  "runtime_execution_state": "running",
  "origin_session_id": "204d5583dad49e8a",
  "parent_session_id": "204d5583dad49e8a"
}
```

## Negative regression checks

- Goal state visibility remained present in initial SessionMap HTML: `Goal Active`, `Goal In Progress`.
- Runtime state visibility remained present in initial SessionMap HTML: `Executing`.
- Routing and session lineage remained present in initial SessionMap HTML and refreshed data:
  - HTML: `from root via 204d5583dad49e8a`
  - overview payload: matching `origin_session_id` and `parent_session_id`
- SessionMap-only permission controls remained present on the selected map-only parent session:
  - `This session is SessionMap-only. Open a working session to edit its goal.`
  - `This session is SessionMap-only. Open a working session for replies.`

## Verification run

- `PYTHONPATH=./src python3 -m unittest tests.test_entrance_page.EntrancePageTests.test_main_page_renders_latest_first_workspace_header_and_fixed_composer -v`
- Result: `OK`

## Remaining risk

- This pass verified authenticated server-rendered HTML plus refreshed overview JSON on an isolated runtime. It did not add a browser-driven click/paint assertion after the `/overview` refresh, so the evidence is strongest for renderer/output correctness rather than an end-to-end DOM mutation trace.
