from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

import fcntl

from wire.protocol import utc_ts


def registry_path(runtime_root: Path) -> Path:
    return runtime_root / "state" / "services.json"


def registry_lock_path(runtime_root: Path) -> Path:
    return runtime_root / "state" / "services.lock"


@contextmanager
def registry_lock(runtime_root: Path):
    lock_path = registry_lock_path(runtime_root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_manifest(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def init_registry(runtime_root: Path, manifest: dict) -> dict:
    with registry_lock(runtime_root):
        existing = _load_registry_unlocked(runtime_root)
        preserve_existing = existing.get("node_id") == manifest["node_id"] and existing.get("run_id") == manifest["run_id"]
        services: dict[str, dict] = dict(existing.get("services", {})) if preserve_existing else {}
        allowed_peers_map: dict[str, list[str]] = {}
        for route in manifest["routes"]:
            if not route.get("enabled", True):
                continue
            allowed_peers_map.setdefault(route["sender_id"], []).append(route["recipient_id"])
        for service in manifest["services"]:
            owner_roles = service.get("owner_roles")
            owner_capabilities = service.get("owner_capabilities")
            existing_record = services.get(service["service_id"], {})
            merged_allowed_peers = sorted(
                {
                    *existing_record.get("allowed_peers", []),
                    *allowed_peers_map.get(service["service_id"], []),
                }
            )
            record = build_service_record(
                service,
                allowed_peers=merged_allowed_peers,
                owner_principal=service.get("owner_principal", existing_record.get("owner_principal", "system")),
                owner_roles=list(owner_roles)
                if isinstance(owner_roles, list)
                else list(existing_record.get("owner_roles", ["system"])),
                owner_capabilities=list(owner_capabilities)
                if isinstance(owner_capabilities, list)
                else list(
                    existing_record.get(
                        "owner_capabilities",
                        ["spawn_service", "manage_users", "superuser"],
                    )
                ),
                status=str(existing_record.get("status", "registered")),
                current_process_id=existing_record.get("current_process_id"),
                created_at=existing_record.get("created_at"),
            )
            services[record["service_id"]] = record
        state = {
            "node_id": manifest["node_id"],
            "run_id": manifest["run_id"],
            "services": services,
        }
        write_registry(runtime_root, state)
        return state


def load_registry(runtime_root: Path) -> dict:
    with registry_lock(runtime_root):
        return _load_registry_unlocked(runtime_root)


def write_registry(runtime_root: Path, state: dict) -> None:
    path = registry_path(runtime_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix="services.", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def update_service_process(
    runtime_root: Path,
    *,
    service_id: str,
    process_id: str | None,
    status: str,
) -> dict:
    with registry_lock(runtime_root):
        state = _load_registry_unlocked(runtime_root)
        service = state["services"][service_id]
        service["current_process_id"] = process_id
        service["status"] = status
        service["updated_at"] = utc_ts()
        write_registry(runtime_root, state)
        return state


def register_service(
    runtime_root: Path,
    *,
    service_spec: dict,
    allowed_peers: list[str] | None = None,
    owner_principal: str | None = None,
    owner_roles: list[str] | None = None,
    owner_capabilities: list[str] | None = None,
) -> dict:
    with registry_lock(runtime_root):
        state = _load_registry_unlocked(runtime_root)
        record = build_service_record(
            service_spec,
            allowed_peers=allowed_peers or [],
            owner_principal=owner_principal,
            owner_roles=owner_roles or [],
            owner_capabilities=owner_capabilities or [],
            created_at=None,
        )
        state["services"][record["service_id"]] = record
        write_registry(runtime_root, state)
        return record


def get_service_record(runtime_root: Path, service_id: str) -> dict:
    with registry_lock(runtime_root):
        return _load_registry_unlocked(runtime_root)["services"][service_id]


def add_allowed_peer(runtime_root: Path, *, service_id: str, peer_service_id: str) -> dict:
    with registry_lock(runtime_root):
        state = _load_registry_unlocked(runtime_root)
        service = state["services"][service_id]
        peers = set(service.get("allowed_peers", []))
        peers.add(peer_service_id)
        service["allowed_peers"] = sorted(peers)
        service["updated_at"] = utc_ts()
        write_registry(runtime_root, state)
        return service


def list_service_records(runtime_root: Path) -> list[dict]:
    with registry_lock(runtime_root):
        state = _load_registry_unlocked(runtime_root)
        return list(state["services"].values())


def _load_registry_unlocked(runtime_root: Path) -> dict:
    path = registry_path(runtime_root)
    if not path.exists():
        return {"services": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def build_service_record(
    service_spec: dict,
    *,
    allowed_peers: list[str],
    owner_principal: str | None,
    owner_roles: list[str],
    owner_capabilities: list[str],
    status: str = "registered",
    current_process_id: str | None = None,
    created_at: str | None,
) -> dict:
    service_id = service_spec["service_id"]
    return {
        "service_id": service_id,
        "kind": service_spec["kind"],
        "display_name": service_spec["display_name"],
        "persona": service_spec["persona"],
        "max_turns": int(service_spec["max_turns"]),
        "response_schema_id": service_spec.get("response_schema_id"),
        "config": dict(service_spec.get("config", {})),
        "status": status,
        "current_process_id": current_process_id,
        "allowed_peers": list(allowed_peers),
        "owner_principal": owner_principal,
        "owner_roles": list(owner_roles),
        "owner_capabilities": list(owner_capabilities),
        "ports": {
            "sock": "ports/router.sock",
        },
        "created_at": created_at or utc_ts(),
        "updated_at": utc_ts(),
    }
