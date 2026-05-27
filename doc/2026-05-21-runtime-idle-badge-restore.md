## Runtime Idle badge restore

- User-visible change: restored explicit `Runtime Idle` wording for idle runtime badges on SessionMap-style surfaces so registered unit sessions keep a clear runtime state label during root-page render and subsequent client-side refreshes.
- Files touched: `src/runtime/http_handler.py`, `src/runtime/html_renderer.py`.
- Verification run: `python3 -m unittest tests.test_http_handler_goal_save -q`, `python3 -m unittest tests.test_goal_manager_compact tests.test_entrance_page -q`, `git diff --check`.
- Remaining risk: running states still render as `Executing`, which matches existing adjacent coverage; broader wording harmonization across non-SessionMap status chips was intentionally left unchanged.
