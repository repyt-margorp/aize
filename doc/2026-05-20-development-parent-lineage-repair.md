# Development Parent Lineage Repair

Changed behavior:
- `resolve_session_template_launch_parent_session_id()` now repairs an already-registered Unit session when the Unit declares a fixed `resident_parent_session_id` but the stored session has lost that Root lineage.
- The repair path reattaches the stored session in both session metadata and DAG state before the resolver returns the launch parent.

Files touched:
- `src/session_template.py`
- `tests/test_session_template.py`

Verification:
- `python3 -m unittest tests.test_session_template.SessionTemplateLauncherTests.test_resolve_bug_hunting_parent_repairs_existing_root_lineage tests.test_session_template.SessionTemplateLauncherTests.test_bug_hunting_unit_provisions_canonical_session_skills tests.test_session_template.SessionTemplateLauncherTests.test_minix_refactor_unit_is_scheduled_development_child`
- `python3 -m py_compile src/session_template.py tests/test_session_template.py`
- Live state reconciliation verified in `.aize-state/sessions/<username>/default/dag/children.json` and `.aize-state/sessions/<username>/59a3a6d6146e301e/dag/parents.json`

Remaining risk:
- The repair path only enforces declared resident-parent lineage. Sessions with an incorrect non-resident parent are not yet actively detached from stale alternate DAG parents.
