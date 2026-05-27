Implemented two compatibility fixes uncovered while verifying the current Entrance and routing bundle.

User-visible behavior changed:
- Plugin unit and legacy app descriptors now expose a stable `unit_id`/`template_id` alias pair during catalog discovery, so launcher and unit catalog consumers can resolve plugin-owned units consistently.
- `create_conversation_session(...)` now accepts the compatibility argument `parent_session_id`, persists it onto the session record, and links the new session into the session DAG after creation.
- Communication routing now materializes canonical development task children under the root-scoped AIzeDevelopment parent using the runtime's unit-owned child-session allowance, so routed Entrance implementation work still lands under the canonical parent even when that root session uses non-user child-creation defaults.

Files touched:
- `src/plugin_catalog.py`
- `src/runtime/http_handler.py`
- `src/runtime/persistent_state_pkg/conversation.py`

Verification run:
- `python3 -m unittest tests.test_plugin_catalog tests.test_service_control`
- `python3 -m unittest tests.test_service_control.ServiceControlParserTests.test_route_spawn_request_to_communication_child_session_creates_canonical_child`
- `python3 -m unittest tests.test_entrance_page tests.test_session_listing tests.test_http_handler_goal_save`
- `python3 -m unittest discover -s tests`

Remaining risk:
- The compatibility path now shares the normal `add_session_child(...)` linkage flow after session creation, and canonical routing relies on the `requester_template_id`/unit-owned exception for root-scoped parent sessions. Future changes to child-session permission rules should keep both paths covered by the routing tests.
