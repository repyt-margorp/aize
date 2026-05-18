from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from kernel.registry import get_service_record, init_registry, update_service_process
from kernel.auth import auth_context_allows
from kernel.router import authorize_control_injection
from wire.protocol import make_message, message_set_meta


class KernelRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.runtime_root = Path(self.tempdir.name)
        init_registry(
            self.runtime_root,
            {
                "node_id": "node-test",
                "run_id": "run-test",
                "routes": [],
                "services": [
                    {
                        "service_id": "service-http-001",
                        "kind": "http",
                        "display_name": "HTTPBridge",
                        "persona": "test",
                        "max_turns": 100,
                        "config": {"port": 4123},
                    }
                ],
            },
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_ignores_stale_terminal_update_for_restarted_service(self) -> None:
        update_service_process(
            self.runtime_root,
            service_id="service-http-001",
            process_id="proc-new",
            status="running",
        )

        update_service_process(
            self.runtime_root,
            service_id="service-http-001",
            process_id="proc-old",
            status="failed",
        )

        record = get_service_record(self.runtime_root, "service-http-001")
        self.assertEqual(record["current_process_id"], "proc-new")
        self.assertEqual(record["status"], "running")

    def test_allows_current_process_to_transition_to_terminal_status(self) -> None:
        update_service_process(
            self.runtime_root,
            service_id="service-http-001",
            process_id="proc-live",
            status="running",
        )

        update_service_process(
            self.runtime_root,
            service_id="service-http-001",
            process_id="proc-live",
            status="stopped",
        )

        record = get_service_record(self.runtime_root, "service-http-001")
        self.assertEqual(record["current_process_id"], "proc-live")
        self.assertEqual(record["status"], "stopped")

    def test_user_role_has_unit_launch_capability_alias(self) -> None:
        auth = {"roles": ["user"], "capabilities": []}

        self.assertTrue(auth_context_allows(auth, "launch_unit"))
        self.assertTrue(auth_context_allows(auth, "launch_session_template"))

    def test_scoped_event_to_http_bridge_bypasses_allowed_peer_check(self) -> None:
        init_registry(
            self.runtime_root,
            {
                "node_id": "node-test",
                "run_id": "run-test",
                "routes": [],
                "services": [
                    {
                        "service_id": "service-http-001",
                        "kind": "http",
                        "display_name": "HTTPBridge",
                        "persona": "test",
                        "max_turns": 100,
                        "config": {"port": 4123},
                    },
                    {
                        "service_id": "service-codex-child-001",
                        "kind": "codex",
                        "display_name": "Child",
                        "persona": "test",
                        "max_turns": 100,
                    },
                ],
            },
        )
        manifest = {"node_id": "node-test"}
        message = make_message(
            from_node_id="node-test",
            from_service_id="service-codex-child-001",
            to_node_id="node-test",
            to_service_id="service-http-001",
            message_type="event",
            run_id="run-test",
            payload={"entry": {"event_type": "service.event"}},
        )
        message_set_meta(message, "conversation", {"username": "test-user", "session_id": "session-1"})

        allowed, reason = authorize_control_injection(
            runtime_root=self.runtime_root,
            manifest=manifest,
            message=message,
        )

        self.assertTrue(allowed)
        self.assertEqual(reason, "scoped_http_event")

    def test_unscoped_prompt_to_http_bridge_still_requires_allowed_peer(self) -> None:
        init_registry(
            self.runtime_root,
            {
                "node_id": "node-test",
                "run_id": "run-test",
                "routes": [],
                "services": [
                    {
                        "service_id": "service-http-001",
                        "kind": "http",
                        "display_name": "HTTPBridge",
                        "persona": "test",
                        "max_turns": 100,
                        "config": {"port": 4123},
                    },
                    {
                        "service_id": "service-codex-child-001",
                        "kind": "codex",
                        "display_name": "Child",
                        "persona": "test",
                        "max_turns": 100,
                    },
                ],
            },
        )
        manifest = {"node_id": "node-test"}
        message = make_message(
            from_node_id="node-test",
            from_service_id="service-codex-child-001",
            to_node_id="node-test",
            to_service_id="service-http-001",
            message_type="prompt",
            run_id="run-test",
            payload={"text": "hello"},
        )

        allowed, reason = authorize_control_injection(
            runtime_root=self.runtime_root,
            manifest=manifest,
            message=message,
        )

        self.assertFalse(allowed)
        self.assertEqual(reason, "recipient_not_allowed_for_sender")
