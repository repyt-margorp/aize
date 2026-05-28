## Behavior Changed

- Verified that the latest Entrance request was received by Entrance and delegated to the `local-unit-registration` child session, rather than being lost at the parent Entrance session.
- Fixed the failing Unit package test fixture so launchable Unit discovery uses the current `unit-package.json` manifest name.
- Cleaned migrated Unit launcher metadata so `package_id` is the single current package field for SessionTemplate state.
- Confirmed the private AIze Development Unit is hidden from the public Unit catalog but remains directly launchable by exact id, while Entrance remains the only public Unit listing.
- Dispatch and lifecycle provider selection now treat recent provider `service.worker_failed` logs with `FileNotFoundError` as fatal for that provider, preventing repeated reassignment to a locally missing provider binary.

## Files Touched

- `src/session_template.py`
- `src/unit_package_catalog.py`
- `src/unit_catalog.py`
- `src/runtime/html_renderer.py`
- `src/runtime/http_handler.py`
- `src/runtime/cli_service_adapter.py`
- `src/runtime/session_lifecycle.py`
- `src/services/svcmgr/loader.py`
- `unit_packages/`
- `tests/test_session_template.py`
- `tests/test_unit_package_catalog.py`
- `tests/test_provider_fatal_logs.py`
- `doc/2026-05-28-entrance-unit-package-dispatch-verification.md`

## Verification Run

- `PYTHONPATH=./src python3 -m unittest tests.test_unit_package_catalog tests.test_session_template -q`
- `PYTHONPATH=./src python3 -m unittest tests.test_service_control tests.test_entrance_page -q`
- `PYTHONPATH=./src python3 -m unittest tests.test_goal_manager_compact -q`
- `PYTHONPATH=./src python3 -m unittest tests.test_provider_fatal_logs -q`
- `PYTHONPATH=./src python3 -m py_compile src/unit_package_catalog.py src/unit_catalog.py src/session_template.py src/services/svcmgr/loader.py`

## Runtime Finding

The observed Entrance delay was not a missing parent dispatch. Entrance created and dispatched `local-unit-registration`; the child then became incomplete because focused verification failed on stale Unit package fixture setup. That verification failure prevented GoalManager from completing the routed work.

## Residual Risk

The live child session may need one more GoalManager pass to observe the now-passing verification result and close itself.
