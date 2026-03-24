"""
Service Manager (svcmgr) — MINIX-inspired RS (Reincarnation Server) equivalent.

Reads service descriptors from src/services/*/service.json and spawns
all enabled services via kernel.spawn messages, then stays alive as
a long-running supervisor.
"""
from __future__ import annotations

import time
from pathlib import Path

from kernel.ipc import RouterConnection
from services.svcmgr.loader import build_service_plan_for_kinds
from wire.protocol import encode_line, make_message, utc_ts, write_jsonl


def run_service(
    *,
    runtime_root: Path,
    manifest: dict,
    self_service: dict,
    process_id: str,
    log_path: Path,
    router_conn: RouterConnection,
    **_kwargs: object,
) -> int:
    """Service Manager: discover, spawn, and supervise all mesh services."""
    node_id = manifest["node_id"]
    run_id = manifest["run_id"]
    service_id = self_service["service_id"]
    svcmgr_cfg = manifest.get("svcmgr", {})
    restart_resume: dict = svcmgr_cfg.get("restart_resume") or {}
    extra_services: list[dict] = svcmgr_cfg.get("extra_services") or []
    extra_routes: list[dict] = svcmgr_cfg.get("extra_routes") or []

    write_jsonl(log_path, {
        "type": "svcmgr.started",
        "ts": utc_ts(),
        "service_id": service_id,
        "process_id": process_id,
    })

    all_specs = sorted(
        build_service_plan_for_kinds(exclude_kinds={"svcmgr"}),
        key=lambda spec: (spec.get("spawn_order", 100), spec["service_id"]),
    )
    planned_service_ids = [str(spec["service_id"]) for spec in all_specs]
    extra_peer_map: dict[str, set[str]] = {}
    for route in extra_routes:
        if not isinstance(route, dict) or not route.get("enabled", True):
            continue
        sender_id = str(route.get("sender_id", "")).strip()
        recipient_id = str(route.get("recipient_id", "")).strip()
        if sender_id and recipient_id:
            extra_peer_map.setdefault(sender_id, set()).add(recipient_id)

    for spec in all_specs:
        config = dict(spec.get("config", {}))
        config["restart_resume"] = restart_resume.get(spec["service_id"])
        spec = {**spec, "config": config}
        allowed_peers = sorted(
            {
                peer_service_id
                for peer_service_id in planned_service_ids
                if peer_service_id != spec["service_id"]
            }
            | extra_peer_map.get(str(spec["service_id"]), set())
        )
        _spawn_service(router_conn, log_path, service_id, node_id, run_id, spec, allowed_peers)

    # Restore extra (dynamically spawned) services from previous run,
    # excluding any already covered by the descriptor-based plan.
    plan_ids = {s["service_id"] for s in all_specs}
    restored_extra_count = 0
    for extra in extra_services:
        if extra["service_id"] in plan_ids:
            continue
        restored_extra_count += 1
        _spawn_service(
            router_conn,
            log_path,
            service_id,
            node_id,
            run_id,
            extra,
            sorted(extra_peer_map.get(str(extra["service_id"]), set())),
        )

    total = len(all_specs) + restored_extra_count
    write_jsonl(log_path, {
        "type": "svcmgr.spawn_complete",
        "ts": utc_ts(),
        "spawned_count": total,
        "planned_count": len(all_specs),
        "extra_count": restored_extra_count,
    })

    # Stay alive as supervisor (MINIX RS-like long-running service)
    while True:
        time.sleep(60)


def _spawn_service(
    router_conn: RouterConnection,
    log_path: Path,
    sender_id: str,
    node_id: str,
    run_id: str,
    spec: dict,
    allowed_peers: list[str],
) -> None:
    msg = make_message(
        from_node_id=node_id,
        from_service_id=sender_id,
        to_node_id=node_id,
        to_service_id="kernel.spawn",
        message_type="service.spawn",
        run_id=run_id,
        payload={
            "service": spec,
            "allowed_peers": allowed_peers,
        },
    )
    router_conn.write(encode_line(msg))
    write_jsonl(log_path, {
        "type": "svcmgr.spawning",
        "ts": utc_ts(),
        "service_id": spec["service_id"],
        "kind": spec["kind"],
    })
    # Brief pause so router can process each spawn before the next
    time.sleep(0.1)
