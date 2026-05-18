from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from cli import run_aize_unit
from cli.run_aize_unit import build_core_manifest
from kernel.lifecycle import load_lifecycle_state, update_process_fields
from kernel.registry import get_service_record, init_registry
from kernel.router import register_delivery_socket, remove_delivery_socket, resolve_repo_root
from kernel.spawn import SpawnManager
from runtime.agent_service import _await_spawn_initial_prompt_route
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

    def test_gemini_descriptor_defaults_to_five_workers(self) -> None:
        descriptor = json.loads((ROOT / "src" / "services" / "gemini" / "service.json").read_text(encoding="utf-8"))
        self.assertEqual(descriptor["kind"], "gemini")
        self.assertEqual(descriptor["pool_size_default"], 5)

    def test_provider_descriptors_grant_spawn_service_capability(self) -> None:
        planned_specs = build_service_plan_for_kinds(exclude_kinds={"svcmgr", "http"})
        for spec in planned_specs:
            if spec["kind"] not in {"codex", "claude", "gemini"}:
                continue
            self.assertIn("spawn_service", spec.get("owner_capabilities", []))

    def test_update_process_fields_tolerates_missing_restart_process_record(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            runtime_root = Path(tempdir)
            process = update_process_fields(
                runtime_root,
                process_id="proc-service-codex-001-restarted",
                fields={"codex_session_id": "thread-123"},
            )

            self.assertEqual(process["process_id"], "proc-service-codex-001-restarted")
            self.assertEqual(process["status"], "unknown")
            self.assertEqual(process["codex_session_id"], "thread-123")
            state = load_lifecycle_state(runtime_root)
            self.assertIn("proc-service-codex-001-restarted", state["processes"])

    def test_runtime_http_scheme_reflects_tls_flag(self) -> None:
        with patch.object(run_aize_unit, "HTTP_TLS", True):
            self.assertEqual(run_aize_unit.runtime_http_scheme(), "https")
        with patch.object(run_aize_unit, "HTTP_TLS", False):
            self.assertEqual(run_aize_unit.runtime_http_scheme(), "http")


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

    def test_spawn_initial_prompt_route_waits_for_registered_child_and_reverse_route(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            runtime_root = Path(tempdir)
            init_registry(
                runtime_root,
                {
                    "node_id": "node-test",
                    "run_id": "run-test",
                    "services": [
                        {
                            "service_id": "service-parent-001",
                            "kind": "codex",
                            "display_name": "Parent",
                            "persona": "parent",
                            "max_turns": 10,
                        }
                    ],
                    "routes": [],
                },
            )

            ready, reason = _await_spawn_initial_prompt_route(
                runtime_root,
                sender_service_id="service-parent-001",
                child_service_id="service-child-001",
                timeout_seconds=0.01,
            )
            self.assertFalse(ready)
            self.assertEqual(reason, "spawn_registration_missing")

            from kernel.registry import register_service

            register_service(
                runtime_root,
                service_spec={
                    "service_id": "service-child-001",
                    "kind": "codex",
                    "display_name": "Child",
                    "persona": "child",
                    "max_turns": 10,
                },
                allowed_peers=[],
            )
            ready, reason = _await_spawn_initial_prompt_route(
                runtime_root,
                sender_service_id="service-parent-001",
                child_service_id="service-child-001",
                timeout_seconds=0.01,
            )
            self.assertFalse(ready)
            self.assertEqual(reason, "spawned_service_missing_reverse_route")

            register_service(
                runtime_root,
                service_spec={
                    "service_id": "service-child-002",
                    "kind": "codex",
                    "display_name": "Child 2",
                    "persona": "child",
                    "max_turns": 10,
                },
                allowed_peers=["service-parent-001"],
            )
            ready, reason = _await_spawn_initial_prompt_route(
                runtime_root,
                sender_service_id="service-parent-001",
                child_service_id="service-child-002",
                timeout_seconds=0.01,
            )
            self.assertTrue(ready)
            self.assertEqual(reason, "ok")

    def test_spawned_service_preserves_spec_owner_capabilities_without_auth(self) -> None:
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
                                "service_id": "service-codex-001",
                                "kind": "codex",
                                "display_name": "Codex",
                                "persona": "worker",
                                "max_turns": 100,
                                "owner_capabilities": ["spawn_service"],
                            },
                            "allowed_peers": ["service-svcmgr-001"],
                        },
                    }
                )

            self.assertEqual(result["type"], "spawn_manager.service_spawned")
            codex_record = get_service_record(runtime_root, "service-codex-001")
            self.assertIn("spawn_service", codex_record["owner_capabilities"])

    def test_spawn_manager_adds_reply_to_service_to_allowed_peers(self) -> None:
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
                        "reply_to_service_id": "service-http-001",
                        "payload": {
                            "service": {
                                "service_id": "service-codex-child-001",
                                "kind": "codex",
                                "display_name": "Child Codex",
                                "persona": "worker",
                                "max_turns": 100,
                            },
                            "allowed_peers": ["service-codex-001"],
                        },
                        "auth": {
                            "principal": "system",
                            "capabilities": ["spawn_service"],
                            "roles": ["system"],
                        },
                    }
                )

            self.assertEqual(result["type"], "spawn_manager.service_spawned")
            child_record = get_service_record(runtime_root, "service-codex-child-001")
            self.assertIn("service-http-001", child_record["allowed_peers"])


class RouterSocketRegistrationTests(unittest.TestCase):
    def test_duplicate_sender_connection_does_not_replace_existing_delivery_socket(self) -> None:
        primary = object()
        duplicate = object()
        write_socks: dict[str, object] = {}

        self.assertTrue(register_delivery_socket(write_socks, sender_id="service-codex-001", sock=primary))
        self.assertFalse(register_delivery_socket(write_socks, sender_id="service-codex-001", sock=duplicate))
        self.assertIs(write_socks["service-codex-001"], primary)

        remove_delivery_socket(write_socks, sender_id="service-codex-001", sock=duplicate)
        self.assertIs(write_socks["service-codex-001"], primary)

        remove_delivery_socket(write_socks, sender_id="service-codex-001", sock=primary)
        self.assertNotIn("service-codex-001", write_socks)


class RouterRootResolutionTests(unittest.TestCase):
    def test_resolve_repo_root_uses_aize_root_when_configured(self) -> None:
        configured_root = ROOT / ".temp" / "router-root-override"
        with patch.dict("os.environ", {"AIZE_ROOT": str(configured_root)}):
            self.assertEqual(resolve_repo_root(), configured_root.resolve())

    def test_resolve_repo_root_defaults_to_repo_root_not_runtime_parent(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(resolve_repo_root(), ROOT.resolve())


if __name__ == "__main__":
    unittest.main()
