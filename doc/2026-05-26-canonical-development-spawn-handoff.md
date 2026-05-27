# Canonical Development Spawn Handoff

## Behavior Changed

Service-control `spawn_requests` emitted from communication sessions that carry a canonical
`create_child_session` routing skill are now handed off through the canonical routed parent
session instead of bypassing session lineage as raw kernel service spawns.

For Entrance development routing, this means development-style delegated work is materialized
under the resident AIze Development parent session when that canonical parent is available.
Normal raw service spawning remains available for sessions without a canonical communication
routing parent.

Explicit service-control spawn handoff does not require `route_when_unhandled=true`. Entrance
keeps ordinary user text local to the communication agents first, while a GoalManager-issued
`spawn_requests` item is treated as the explicit delegation decision and may use the canonical
`create_child_session` route.

## Files Touched

- `src/runtime/agent_service.py`
- `tests/test_service_control.py`

## Verification

- `PYTHONPATH=./src python3 -m unittest tests.test_service_control.ServiceControlParserTests.test_route_spawn_request_to_communication_child_session_creates_canonical_child tests.test_service_control.ServiceControlParserTests.test_handoff_spawn_request_uses_canonical_development_parent_for_communication_session tests.test_service_control.ServiceControlParserTests.test_communication_spawn_handoff_uses_canonical_development_parent -v`
- `PYTHONPATH=./src python3 -m unittest tests.test_entrance_page.EntrancePageTests.test_materialize_communication_routed_child_session_creates_canonical_development_session tests.test_entrance_page.EntrancePageTests.test_materialize_communication_routed_child_session_reuses_registered_parent_for_parallel_tasks tests.test_entrance_page.EntrancePageTests.test_materialize_communication_routed_child_session_prefers_registered_parent_even_when_parent_goal_is_complete -v`

The checks were run with `unittest` directly.

## Remaining Risk

The fix changes future spawn handling only. Already-running ad hoc services from previous
runtime state are not reparented or converted into child sessions.
