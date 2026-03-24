from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from cli.run_codex_http_mesh import build_core_manifest
from kernel.registry import get_service_record, init_registry
from kernel.spawn import SpawnManager
from services.svcmgr import run_service
from services.svcmgr.loader import build_service_plan_for_kinds
from wire.protocol import decode_line


class BootstrapManifestTests(unittest.TestCase):
    def test_core_manifest_bootstraps_only_service_manager(self) -> None:
        manifest = build_core_manifest(
            extra_services=[{"service_id": "service-worker-001", "kind": "codex"}],
            extra_routes=[{"sender_id": "service-http-001", "recipient_id": "service-worker-001", "enabled": True}],
            restart_resume={"service-http-001": {"mode": "system_restart"}},
        )

        self.assertEqual([service["service_id"] for service in manifest["services"]], ["service-svcmgr-001"])
        self.assertEqual(manifest["routes"], [])
        self.assertEqual(
            manifest["svcmgr"],
            {
                "restart_resume": {"service-http-001": {"mode": "system_restart"}},
                "extra_services": [{"service_id": "service-worker-001", "kind": "codex"}],
                "extra_routes": [
                    {"sender_id": "service-http-001", "recipient_id": "service-worker-001", "enabled": True}
                ],
            },
        )


class ServiceManagerSpawnTests(unittest.TestCase):
    def test_service_manager_spawns_descriptor_managed_services(self) -> None:
        planned_specs = sorted(
            build_service_plan_for_kinds(exclude_kinds={"svcmgr"}),
            key=lambda spec: (spec.get("spawn_order", 100), spec["service_id"]),
        )
        planned_service_ids = [spec["service_id"] for spec in planned_specs]

        sent_messages: list[dict] = []

        class FakeRouterConnection:
            def write(self, line: str) -> None:
                sent_messages.append(decode_line(line))

        with tempfile.TemporaryDirectory() as tempdir:
            runtime_root = Path(tempdir)
            log_path = runtime_root / "logs" / "service-svcmgr-001.jsonl"
            sleep_calls = {"count": 0}

            def fake_sleep(_seconds: float) -> None:
                sleep_calls["count"] += 1
                if sleep_calls["count"] > len(planned_specs):
                    raise RuntimeError("stop supervisor loop")

            with patch("services.svcmgr.time.sleep", side_effect=fake_sleep):
                with self.assertRaisesRegex(RuntimeError, "stop supervisor loop"):
                    run_service(
                        runtime_root=runtime_root,
                        manifest={
                            "node_id": "node-aize",
                            "run_id": "run-test",
                            "svcmgr": {},
                        },
                        self_service={"service_id": "service-svcmgr-001"},
                        process_id="proc-svcmgr-test",
                        log_path=log_path,
                        router_conn=FakeRouterConnection(),
                    )

        self.assertEqual(len(sent_messages), len(planned_specs))
        self.assertEqual(sent_messages[0]["payload"]["service"]["service_id"], "service-http-001")

        for message in sent_messages:
            self.assertEqual(message["type"], "service.spawn")
            service = message["payload"]["service"]
            service_id = service["service_id"]
            allowed_peers = sorted(message["payload"]["allowed_peers"])
            self.assertEqual(
                allowed_peers,
                sorted(peer for peer in planned_service_ids if peer != service_id),
            )


class SpawnManagerBootstrapTests(unittest.TestCase):
    def test_spawn_manager_ignores_reverse_routes_for_unregistered_peers(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            runtime_root = Path(tempdir)
            manifest = build_core_manifest()
            init_registry(runtime_root, manifest)
            manifest_path = runtime_root / "manifest.json"
            manifest_path.write_text("{}", encoding="utf-8")

            manager = SpawnManager(
                runtime_root=runtime_root,
                manifest_path=manifest_path,
                root_dir=Path(tempdir),
                write_socks={},
            )

            with patch.object(manager, "_spawn_adapter"), patch.object(manager, "_attach_service_ports"):
                result = manager.spawn_from_message(
                    {
                        "from": "service-svcmgr-001",
                        "payload": {
                            "service": {
                                "service_id": "service-http-001",
                                "kind": "http",
                                "display_name": "HttpBridge",
                                "persona": "bridge",
                                "max_turns": 100,
                            },
                            "allowed_peers": ["service-claude-001", "service-svcmgr-001"],
                        },
                        "_meta": {
                            "auth": {
                                "principal": "system",
                                "capabilities": ["spawn_service"],
                                "roles": ["system"],
                            }
                        },
                    }
                )

            self.assertEqual(result["type"], "spawn_manager.service_spawned")
            http_record = get_service_record(runtime_root, "service-http-001")
            self.assertIn("service-claude-001", http_record["allowed_peers"])
            svcmgr_record = get_service_record(runtime_root, "service-svcmgr-001")
            self.assertIn("service-http-001", svcmgr_record["allowed_peers"])


if __name__ == "__main__":
    unittest.main()
