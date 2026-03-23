from __future__ import annotations

import argparse
import json
import os
import select
import sys
import urllib.error
import urllib.request
from pathlib import Path

from kernel.lifecycle import init_lifecycle_state
from kernel.peers import get_peer
from kernel.registry import get_service_record, init_registry, list_service_records, load_manifest, update_service_process
from kernel.spawn import SpawnManager
from wire.protocol import decode_line, encode_line, message_meta_get, utc_ts, write_jsonl


def open_fifo_read(path: Path) -> int:
    return os.open(path, os.O_RDWR | os.O_NONBLOCK)


def open_fifo_write(path: Path) -> int:
    return os.open(path, os.O_RDWR | os.O_NONBLOCK)

def record_payload_mode(message: dict) -> str:
    return "ref" if message.get("payload_ref") else "inline"


def is_local_message(manifest: dict, message: dict) -> bool:
    return str(message_meta_get(message, "to_node", manifest["node_id"])) == str(manifest["node_id"])


CONTROL_MESSAGE_TYPES = {"service.start", "service.stop", "service.restart", "service.reload", "service.status"}
KERNEL_SPAWN_RECIPIENT = "kernel.spawn"
KERNEL_CONTROL_RECIPIENT = "kernel.control"


def has_core_message_fields(message: dict) -> bool:
    return all(key in message for key in ("from", "to", "type"))


def parse_service_done(message: dict) -> tuple[str | None, str | None]:
    payload = message.get("payload")
    if isinstance(payload, dict):
        service_id = payload.get("service_id")
        process_id = payload.get("process_id")
        return (
            str(service_id) if isinstance(service_id, str) and service_id else None,
            str(process_id) if isinstance(process_id, str) and process_id else None,
        )
    service_id = message.get("service_id")
    process_id = message.get("process_id")
    return (
        str(service_id) if isinstance(service_id, str) and service_id else None,
        str(process_id) if isinstance(process_id, str) and process_id else None,
    )


def route_kernel_message(
    *,
    router_log: Path,
    spawn_manager: SpawnManager,
    message: dict,
) -> bool:
    if not has_core_message_fields(message):
        write_jsonl(
            router_log,
            {
                "type": "router.kernel_message_rejected",
                "ts": utc_ts(),
                "reason": "missing_required_fields",
                "message": message,
            },
        )
        return True

    recipient_id = str(message.get("to", ""))
    message_type = str(message.get("type", ""))
    if recipient_id == KERNEL_SPAWN_RECIPIENT:
        if message_type != "service.spawn":
            write_jsonl(
                router_log,
                {
                    "type": "router.kernel_message_rejected",
                    "ts": utc_ts(),
                    "reason": "unsupported_kernel_spawn_message",
                    "message": message,
                },
            )
            return True
        write_jsonl(router_log, spawn_manager.spawn_from_message(message))
        return True

    if recipient_id == KERNEL_CONTROL_RECIPIENT:
        if message_type not in CONTROL_MESSAGE_TYPES:
            write_jsonl(
                router_log,
                {
                    "type": "router.kernel_message_rejected",
                    "ts": utc_ts(),
                    "reason": "unsupported_kernel_control_message",
                    "message": message,
                },
            )
            return True
        write_jsonl(router_log, spawn_manager.control_from_message(message))
        return True

    if message_type == "service.spawn" or message_type in CONTROL_MESSAGE_TYPES:
        write_jsonl(
            router_log,
            {
                "type": "router.kernel_message_rejected",
                "ts": utc_ts(),
                "reason": "kernel_message_requires_kernel_recipient",
                "message": message,
            },
        )
        return True

    return False


def authorize_control_injection(
    *,
    runtime_root: Path,
    manifest: dict,
    message: dict,
) -> tuple[bool, str]:
    if not has_core_message_fields(message):
        return False, "missing_required_fields"

    sender_id = str(message.get("from", ""))
    recipient_id = str(message.get("to", ""))
    from_node = str(message_meta_get(message, "from_node", manifest["node_id"]))
    if from_node != str(manifest["node_id"]):
        return False, "remote_control_injection_disabled"

    if sender_id in {"user.local", "kernel.local"}:
        return True, "system_sender"

    try:
        sender_record = get_service_record(runtime_root, sender_id)
    except KeyError:
        return False, "unknown_sender"

    if recipient_id == sender_id:
        return True, "self_delivery"

    if recipient_id in {KERNEL_SPAWN_RECIPIENT, KERNEL_CONTROL_RECIPIENT}:
        return True, "known_service_to_kernel"

    allowed_peers = set(sender_record.get("allowed_peers", []))
    if recipient_id not in allowed_peers:
        return False, "recipient_not_allowed_for_sender"

    return True, "known_service_to_allowed_peer"


def forward_remote_message(runtime_root: Path, message: dict) -> tuple[bool, str]:
    to_node = str(message_meta_get(message, "to_node", ""))
    peer = get_peer(runtime_root, to_node)
    if not peer:
        return False, f"unknown_peer:{to_node}"
    request = urllib.request.Request(
        url=f"{peer['base_url']}/federation/message",
        data=json.dumps({"message": message}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            if 200 <= response.status < 300:
                return True, peer["base_url"]
            return False, f"http_status:{response.status}"
    except urllib.error.URLError as exc:
        return False, str(exc)


def deliver_local_message(
    *,
    router_log: Path,
    write_fds: dict[str, int],
    recipient_id: str,
    message: dict,
    manifest: dict,
) -> bool:
    fd = write_fds.get(recipient_id)
    if fd is None:
        write_jsonl(
            router_log,
            {
                "type": "router.delivery_deferred",
                "ts": utc_ts(),
                "reason": "recipient_unavailable",
                "recipient_id": recipient_id,
                "message": message,
            },
        )
        return False
    try:
        os.write(fd, encode_line(message).encode("utf-8"))
        return True
    except OSError as exc:
        write_jsonl(
            router_log,
            {
                "type": "router.delivery_deferred",
                "ts": utc_ts(),
                "reason": "write_failed",
                "recipient_id": recipient_id,
                "detail": str(exc),
                "message": message,
            },
        )
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Local mesh router")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--runtime-root", required=True)
    args = parser.parse_args()

    runtime_root = Path(args.runtime_root)
    manifest = load_manifest(Path(args.manifest))
    init_registry(runtime_root, manifest)
    init_lifecycle_state(runtime_root, node_id=manifest["node_id"], run_id=manifest["run_id"])
    logs_dir = runtime_root / "logs"
    ports_dir = runtime_root / "ports"
    router_log = logs_dir / "router.jsonl"
    control_port = ports_dir / "router.control"

    write_jsonl(
        router_log,
        {
            "type": "router.started",
            "ts": utc_ts(),
            "node_id": manifest["node_id"],
            "runtime_root": str(runtime_root),
            "run_id": manifest["run_id"],
        },
    )

    control_fd = open_fifo_read(control_port)
    read_fds: dict[int, tuple[str, Path]] = {control_fd: ("control", control_port)}
    write_fds: dict[str, int] = {}
    buffers: dict[int, str] = {}
    spawn_manager = SpawnManager(
        runtime_root=runtime_root,
        manifest_path=Path(args.manifest),
        root_dir=Path(args.manifest).resolve().parent.parent,
        read_fds=read_fds,
        write_fds=write_fds,
        buffers=buffers,
    )
    spawn_manager.attach_existing_services()

    done_services: set[str] = {
        str(record["service_id"])
        for record in list_service_records(runtime_root)
        if str(record.get("status", "")) == "stopped"
    }

    while True:
        ready, _, _ = select.select(list(read_fds.keys()), [], [], 1.0)
        for fd in ready:
            try:
                chunk = os.read(fd, 65536).decode("utf-8")
            except BlockingIOError:
                continue
            if not chunk:
                continue
            buffers[fd] = buffers.get(fd, "") + chunk
            while "\n" in buffers[fd]:
                line, buffers[fd] = buffers[fd].split("\n", 1)
                if not line.strip():
                    continue
                message = decode_line(line)
                source_kind, _ = read_fds[fd]

                if source_kind == "control":
                    allowed, detail = authorize_control_injection(
                        runtime_root=runtime_root,
                        manifest=manifest,
                        message=message,
                    )
                    if not allowed:
                        write_jsonl(
                            router_log,
                            {
                                "type": "router.control_injection_rejected",
                                "ts": utc_ts(),
                                "reason": detail,
                                "message": message,
                            },
                        )
                        continue
                    if message.get("type") == "service.done":
                        write_jsonl(
                            router_log,
                            {
                                "type": "router.control_injection_rejected",
                                "ts": utc_ts(),
                                "reason": "service_done_not_injectable",
                                "message": message,
                            },
                        )
                        continue
                    if route_kernel_message(
                        router_log=router_log,
                        spawn_manager=spawn_manager,
                        message=message,
                    ):
                        continue
                    if not is_local_message(manifest, message):
                        delivered, detail = forward_remote_message(runtime_root, message)
                        write_jsonl(
                            router_log,
                            {
                                "type": "router.remote_message_deferred",
                                "ts": utc_ts(),
                                "to_node": message_meta_get(message, "to_node"),
                                "delivered": delivered,
                                "detail": detail,
                                "message": message,
                            },
                        )
                        continue
                    recipient_id = message["to"]
                    deliver_local_message(
                        router_log=router_log,
                        write_fds=write_fds,
                        recipient_id=recipient_id,
                        message=message,
                        manifest=manifest,
                    )
                    write_jsonl(
                        router_log,
                        {
                            "type": "router.message_injected",
                            "ts": utc_ts(),
                            "payload_mode": record_payload_mode(message),
                            "from_process_id": message_meta_get(message, "process_id"),
                            "message": message,
                        },
                    )
                    continue

                if message.get("type") == "service.done":
                    service_id, process_id = parse_service_done(message)
                    if not service_id:
                        write_jsonl(
                            router_log,
                            {
                                "type": "router.kernel_message_rejected",
                                "ts": utc_ts(),
                                "reason": "service_done_missing_service_id",
                                "message": message,
                            },
                        )
                        continue
                    done_services.add(service_id)
                    update_service_process(
                        runtime_root,
                        service_id=service_id,
                        process_id=process_id or message_meta_get(message, "process_id"),
                        status="stopped",
                    )
                    write_jsonl(
                        router_log,
                        {
                            "type": "router.service_done",
                            "ts": utc_ts(),
                            "service_id": service_id,
                            "process_id": process_id or message_meta_get(message, "process_id"),
                        },
                    )
                    continue

                if route_kernel_message(
                    router_log=router_log,
                    spawn_manager=spawn_manager,
                    message=message,
                ):
                    continue

                sender_id = message["from"]
                recipient_id = message["to"]
                allowed, _reason = authorize_control_injection(
                    runtime_root=runtime_root,
                    manifest=manifest,
                    message=message,
                )
                try:
                    sender_record = get_service_record(runtime_root, sender_id)
                    sender_registered = True
                except KeyError:
                    sender_record = None
                    sender_registered = False
                local_delivery = is_local_message(manifest, message)
                write_jsonl(
                    router_log,
                    {
                        "type": "router.message_forward_attempt",
                        "ts": utc_ts(),
                        "allowed": allowed,
                        "reason": _reason,
                        "sender_registered": sender_registered,
                        "local_delivery": local_delivery,
                        "payload_mode": record_payload_mode(message),
                        "from_process_id": message_meta_get(message, "process_id"),
                        "message": message,
                    },
                )
                if not allowed:
                    continue
                if not local_delivery:
                    delivered, detail = forward_remote_message(runtime_root, message)
                    write_jsonl(
                        router_log,
                        {
                            "type": "router.remote_message_deferred",
                            "ts": utc_ts(),
                            "to_node": message_meta_get(message, "to_node"),
                            "delivered": delivered,
                            "detail": detail,
                            "message": message,
                        },
                    )
                    continue
                deliver_local_message(
                    router_log=router_log,
                    write_fds=write_fds,
                    recipient_id=recipient_id,
                    message=message,
                    manifest=manifest,
                )

        if len(done_services) == len(list_service_records(runtime_root)):
            write_jsonl(
                router_log,
                {
                    "type": "router.stopped",
                    "ts": utc_ts(),
                    "reason": "all_services_done",
                },
            )
            return 0


if __name__ == "__main__":
    sys.exit(main())
