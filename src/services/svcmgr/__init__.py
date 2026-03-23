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
from services.svcmgr.loader import build_service_plan, list_service_descriptors
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

    write_jsonl(log_path, {
        "type": "svcmgr.started",
        "ts": utc_ts(),
        "service_id": service_id,
        "process_id": process_id,
    })

    descriptors = list_service_descriptors()
    all_specs = build_service_plan(descriptors)

    # Split into two spawn waves based on spawn_order.
    # Wave 1 (spawn_order < 50): front-end / gateway services (e.g. HttpBridge).
    # Wave 2 (spawn_order >= 50): back-end / agent services.
    # Each wave gets the other wave's service IDs as allowed peers so messages
    # can flow bidirectionally without hardcoding service kinds in this manager.
    _WAVE_THRESHOLD = 50
    all_specs_ordered = sorted(all_specs, key=lambda s: s.get("spawn_order", 100))
    wave1 = [s for s in all_specs_ordered if s.get("spawn_order", 100) < _WAVE_THRESHOLD]
    wave2 = [s for s in all_specs_ordered if s.get("spawn_order", 100) >= _WAVE_THRESHOLD]
    wave1_ids = [s["service_id"] for s in wave1]
    wave2_ids = [s["service_id"] for s in wave2]

    # Spawn wave 1 first so reverse routes from wave 2 can be registered
    for spec in wave1:
        config = dict(spec.get("config", {}))
        config["restart_resume"] = restart_resume.get(spec["service_id"])
        spec = {**spec, "config": config}
        _spawn_service(router_conn, log_path, service_id, node_id, run_id, spec, wave2_ids)

    # Spawn wave 2 with wave 1 as allowed peers
    for spec in wave2:
        config = dict(spec.get("config", {}))
        config["restart_resume"] = restart_resume.get(spec["service_id"])
        spec = {**spec, "config": config}
        _spawn_service(router_conn, log_path, service_id, node_id, run_id, spec, wave1_ids)

    # Restore extra (dynamically spawned) services from previous run,
    # excluding any already covered by the descriptor-based plan.
    plan_ids = {s["service_id"] for s in all_specs}
    for extra in extra_services:
        if extra["service_id"] in plan_ids:
            continue
        _spawn_service(router_conn, log_path, service_id, node_id, run_id, extra, [])

    total = len(all_specs) + len(extra_services)
    write_jsonl(log_path, {
        "type": "svcmgr.spawn_complete",
        "ts": utc_ts(),
        "spawned_count": total,
        "wave1_count": len(wave1),
        "wave2_count": len(wave2),
        "extra_count": len(extra_services),
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
