from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from kernel.registry import init_registry
from kernel.router import authorize_control_injection
from kernel.ws_transport import (
    KERNEL_WS_TRANSPORT,
    authorize_inbound_kernel_message,
    mark_inbound_kernel_transport,
    ws_url_from_peer_record,
)


class WsKernelTransportPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.runtime_root = Path(self.tempdir.name)
        self.manifest = {
            "node_id": "node-local",
            "run_id": "run-test",
            "services": [
                {
                    "service_id": "service-http-001",
                    "kind": "http",
                    "display_name": "HTTP",
                    "persona": "HTTP bridge",
                    "max_turns": 100,
                    "allowed_peers": ["service-codex-001"],
                },
                {
                    "service_id": "service-codex-001",
                    "kind": "codex",
                    "display_name": "Codex",
                    "persona": "Codex worker",
                    "max_turns": 100,
                    "allowed_peers": ["service-http-001"],
                },
            ],
            "routes": [
                {
                    "sender_id": "service-http-001",
                    "recipient_id": "service-codex-001",
                    "enabled": True,
                }
            ],
        }
        init_registry(self.runtime_root, self.manifest)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_inbound_kernel_message_requires_trusted_node_policy(self) -> None:
        (self.runtime_root / "ws_router_peers.json").write_text(
            json.dumps(
                [
                    {
                        "node_id": "node-remote",
                        "target_ws_url": "ws://remote.example/ws",
                        "accept_from_nodes": ["node-remote"],
                        "accept_to_services": ["service-codex-001"],
                        "accept_message_types": ["dispatch_pending"],
                    }
                ]
            ),
            encoding="utf-8",
        )
        message = {
            "from": "service-http-001",
            "to": "service-codex-001",
            "type": "dispatch_pending",
            "meta": {"from_node": "node-remote", "to_node": "node-local"},
        }

        allowed, reason = authorize_inbound_kernel_message(
            self.runtime_root,
            manifest=self.manifest,
            auth_context={"node_id": "node-remote"},
            message=message,
        )

        self.assertTrue(allowed, reason)

    def test_inbound_kernel_message_rejects_unlisted_recipient(self) -> None:
        (self.runtime_root / "ws_router_peers.json").write_text(
            json.dumps(
                [
                    {
                        "node_id": "node-remote",
                        "target_ws_url": "ws://remote.example/ws",
                        "accept_to_services": ["service-codex-001"],
                    }
                ]
            ),
            encoding="utf-8",
        )
        message = {
            "from": "service-http-001",
            "to": "service-claude-001",
            "type": "dispatch_pending",
            "meta": {"from_node": "node-remote", "to_node": "node-local"},
        }

        allowed, reason = authorize_inbound_kernel_message(
            self.runtime_root,
            manifest=self.manifest,
            auth_context={"node_id": "node-remote"},
            message=message,
        )

        self.assertFalse(allowed)
        self.assertEqual(reason, "recipient_not_allowed")

    def test_router_accepts_remote_message_only_after_ws_transport_marks_it(self) -> None:
        message = {
            "from": "service-http-001",
            "to": "service-codex-001",
            "type": "dispatch_pending",
            "meta": {"from_node": "node-remote", "to_node": "node-local"},
        }
        allowed, reason = authorize_control_injection(
            runtime_root=self.runtime_root,
            manifest=self.manifest,
            message=message,
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, "remote_control_injection_disabled")

        marked = mark_inbound_kernel_transport(message, peer_username="remote-user")
        allowed, reason = authorize_control_injection(
            runtime_root=self.runtime_root,
            manifest=self.manifest,
            message=marked,
        )
        self.assertTrue(allowed, reason)
        self.assertEqual(marked["meta"]["ingress_transport"], KERNEL_WS_TRANSPORT)

    def test_ws_url_is_derived_from_federation_base_url(self) -> None:
        self.assertEqual(
            ws_url_from_peer_record({"base_url": "https://example.test:4123"}),
            "wss://example.test:4123/ws",
        )
        self.assertEqual(
            ws_url_from_peer_record({"base_url": "http://example.test:4123"}),
            "ws://example.test:4123/ws",
        )


if __name__ == "__main__":
    unittest.main()
