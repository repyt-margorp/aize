from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
from pathlib import Path

from kernel.auth import auth_context_allows
from kernel.lifecycle import get_process_record, update_process_fields
from kernel.registry import add_allowed_peer, get_service_record, list_service_records, register_service
from wire.protocol import message_meta_get, utc_ts


class SpawnManager:
    def __init__(
        self,
        *,
        runtime_root: Path,
        manifest_path: Path,
        root_dir: Path,
        write_socks: dict[str, "socket.socket"],
    ) -> None:
        self.runtime_root = runtime_root
        self.manifest_path = manifest_path
        self.root_dir = root_dir
        self.write_socks = write_socks
        self.child_processes: list[subprocess.Popen] = []

    def attach_existing_services(self) -> None:
        # Services connect to router.sock dynamically on spawn; no-op here.
        pass

    def spawn_from_message(self, message: dict) -> dict:
        sender = str(message.get("from", ""))
        incoming_auth = message_meta_get(message, "auth")
        auth = incoming_auth if isinstance(incoming_auth, dict) else {}
        if sender not in ("user.local", "kernel.local") and not auth_context_allows(auth, "spawn_service"):
            # Allow if the sender's registered service record grants spawn_service
            try:
                sender_record = get_service_record(self.runtime_root, sender)
                if "spawn_service" not in sender_record.get("owner_capabilities", []):
                    return {
                        "type": "spawn_manager.spawn_rejected",
                        "ts": utc_ts(),
                        "service_id": message.get("payload", {}).get("service", {}).get("service_id"),
                        "spawned_by": sender,
                        "reason": "spawn_service_capability_required",
                    }
            except KeyError:
                return {
                    "type": "spawn_manager.spawn_rejected",
                    "ts": utc_ts(),
                    "service_id": message.get("payload", {}).get("service", {}).get("service_id"),
                    "spawned_by": sender,
                    "reason": "spawn_service_capability_required",
                }
        payload = message.get("payload", {})
        service_spec = dict(payload["service"])
        allowed_peers = list(payload.get("allowed_peers") or [])
        if message.get("from") not in ("user.local", "kernel.local"):
            allowed_peers = sorted({*allowed_peers, str(message["from"])})

        service_record = register_service(
            self.runtime_root,
            service_spec=service_spec,
            allowed_peers=allowed_peers,
            owner_principal=str(auth.get("principal")) if auth.get("principal") else sender,
            owner_roles=[str(item) for item in auth.get("roles", []) if isinstance(item, str)],
            owner_capabilities=[str(item) for item in auth.get("capabilities", []) if isinstance(item, str)],
        )
        self._create_service_ports(service_record)
        self._add_reverse_routes(service_record["service_id"], allowed_peers)
        self._spawn_adapter(service_record["service_id"])
        self._attach_service_ports(service_record)
        return {
            "type": "spawn_manager.service_spawned",
            "ts": utc_ts(),
            "service_id": service_record["service_id"],
            "kind": service_record["kind"],
            "allowed_peers": service_record["allowed_peers"],
            "owner_principal": service_record.get("owner_principal"),
            "spawned_by": message.get("from"),
        }

    def control_from_message(self, message: dict) -> dict:
        incoming_auth = message_meta_get(message, "auth")
        auth = incoming_auth if isinstance(incoming_auth, dict) else {}
        action = str(message.get("type", ""))
        service_id = str(message.get("payload", {}).get("service_id", ""))
        if action not in {"service.start", "service.stop", "service.restart", "service.reload", "service.status"}:
            return {"type": "service.control_rejected", "ts": utc_ts(), "reason": "unsupported_action", "action": action}
        if not service_id:
            return {"type": "service.control_rejected", "ts": utc_ts(), "reason": "service_id_required", "action": action}
        required_capability = "read_service_status" if action == "service.status" else "control_service"
        if str(message.get("from", "")) not in ("user.local", "kernel.local") and not auth_context_allows(auth, required_capability):
            return {
                "type": "service.control_rejected",
                "ts": utc_ts(),
                "reason": f"{required_capability}_required",
                "action": action,
                "service_id": service_id,
            }
        if action == "service.status":
            record = get_service_record(self.runtime_root, service_id)
            process = self._current_process_record(record)
            return {
                "type": "service.status.result",
                "ts": utc_ts(),
                "service_id": service_id,
                "service": record,
                "process": process,
            }
        if action == "service.start":
            return self._start_service(service_id)
        if action == "service.stop":
            return self._stop_service(service_id)
        if action == "service.restart":
            stop_result = self._stop_service(service_id)
            start_result = self._start_service(service_id)
            return {
                "type": "service.restart.result",
                "ts": utc_ts(),
                "service_id": service_id,
                "stop": stop_result,
                "start": start_result,
            }
        return self._reload_service(service_id)

    def _create_service_ports(self, service_record: dict) -> None:
        # No FIFO creation needed; services connect via UNIX socket.
        pass

    def _add_reverse_routes(self, service_id: str, allowed_peers: list[str]) -> None:
        for peer_service_id in allowed_peers:
            if peer_service_id.startswith("service-"):
                try:
                    get_service_record(self.runtime_root, peer_service_id)
                except KeyError:
                    # During svcmgr bootstrap, peers may not be registered yet.
                    # They will pick up their reverse routes when they spawn later.
                    continue
                add_allowed_peer(
                    self.runtime_root,
                    service_id=peer_service_id,
                    peer_service_id=service_id,
                )

    def _spawn_adapter(self, service_id: str) -> None:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(self.root_dir / "src")
        logs_dir = self.runtime_root / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        stdout_handle = (logs_dir / f"{service_id}.stdout.log").open("a", encoding="utf-8")
        stderr_handle = (logs_dir / f"{service_id}.stderr.log").open("a", encoding="utf-8")
        child = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "runtime.cli_service_adapter",
                "--manifest",
                str(self.manifest_path),
                "--runtime-root",
                str(self.runtime_root),
                "--service-id",
                service_id,
            ],
            cwd=str(self.root_dir),
            env=env,
            stdout=stdout_handle,
            stderr=stderr_handle,
            text=True,
            encoding="utf-8",
        )
        child._aize_stdout_handle = stdout_handle  # type: ignore[attr-defined]
        child._aize_stderr_handle = stderr_handle  # type: ignore[attr-defined]
        self.child_processes.append(child)

    def _attach_service_ports(self, service_record: dict) -> None:
        # Services connect to router.sock dynamically; no FIFO attachment needed.
        pass

    def _current_process_record(self, service_record: dict) -> dict | None:
        process_id = service_record.get("current_process_id")
        if not process_id:
            return None
        try:
            return get_process_record(self.runtime_root, str(process_id))
        except KeyError:
            return None

    def _service_os_pid(self, service_record: dict) -> int | None:
        process = self._current_process_record(service_record)
        if not process:
            return None
        os_pid = process.get("os_pid")
        return int(os_pid) if isinstance(os_pid, int) or (isinstance(os_pid, str) and str(os_pid).isdigit()) else None

    def _start_service(self, service_id: str) -> dict:
        record = get_service_record(self.runtime_root, service_id)
        os_pid = self._service_os_pid(record)
        if os_pid:
            try:
                os.kill(os_pid, 0)
                return {"type": "service.start.result", "ts": utc_ts(), "service_id": service_id, "ok": False, "reason": "already_running"}
            except OSError:
                pass
        self._spawn_adapter(service_id)
        return {"type": "service.start.result", "ts": utc_ts(), "service_id": service_id, "ok": True}

    def _stop_service(self, service_id: str) -> dict:
        record = get_service_record(self.runtime_root, service_id)
        process = self._current_process_record(record)
        os_pid = self._service_os_pid(record)
        if not os_pid:
            return {"type": "service.stop.result", "ts": utc_ts(), "service_id": service_id, "ok": False, "reason": "not_running"}
        try:
            os.kill(os_pid, signal.SIGTERM)
        except OSError as exc:
            return {"type": "service.stop.result", "ts": utc_ts(), "service_id": service_id, "ok": False, "reason": str(exc)}
        if process:
            update_process_fields(self.runtime_root, process_id=str(process["process_id"]), fields={"status": "stopping", "signal": "SIGTERM"})
        return {"type": "service.stop.result", "ts": utc_ts(), "service_id": service_id, "ok": True}

    def _reload_service(self, service_id: str) -> dict:
        record = get_service_record(self.runtime_root, service_id)
        process = self._current_process_record(record)
        os_pid = self._service_os_pid(record)
        if not os_pid:
            return {"type": "service.reload.result", "ts": utc_ts(), "service_id": service_id, "ok": False, "reason": "not_running"}
        try:
            os.kill(os_pid, signal.SIGHUP)
        except OSError as exc:
            return {"type": "service.reload.result", "ts": utc_ts(), "service_id": service_id, "ok": False, "reason": str(exc)}
        if process:
            update_process_fields(self.runtime_root, process_id=str(process["process_id"]), fields={"last_signal": "SIGHUP"})
        return {"type": "service.reload.result", "ts": utc_ts(), "service_id": service_id, "ok": True}
