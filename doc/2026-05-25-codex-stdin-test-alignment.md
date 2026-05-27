## Codex stdin test alignment

- User-visible behavior: none changed. Codex prompt transport remains stdin-based for fresh and resumed `codex exec` calls so large AIze envelopes do not overflow argv limits.
- Files touched: `tests/test_goal_manager_compact.py`.
- Verification run: `python3 -m unittest tests.test_goal_manager_compact tests.test_entrance_page tests.test_session_listing -q`, `python3 -m unittest tests.test_http_handler_goal_save -q`, `git diff --check -- tests/test_goal_manager_compact.py doc/2026-05-25-codex-stdin-test-alignment.md`.
- Remaining risk: the tests cover stdin write/close behavior with fakes, but they still do not exercise a real Codex CLI process in unit scope.
