from __future__ import annotations

import base64
import json
import os
import socket
import ssl
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from kernel.peers import get_peer, load_peers
from runtime.ws_bridge import OP_CLOSE, OP_PING, OP_TEXT, read_frame, write_masked_text_frame
from wire.protocol import message_meta_get, message_set_meta

KERNEL_WS_MESSAGE_TYPE = "kernel.message"
KERNEL_WS_ACCEPTED_TYPE = "kernel.message_accepted"
KERNEL_WS_TRANSPORT = "ws.kernel"


def ws_router_peers_path(runtime_root: Path) -> Path:
    return runtime_root / "ws_router_peers.json"


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def load_ws_router_peer_configs(runtime_root: Path) -> dict[str, dict[str, Any]]:
    path = ws_router_peers_path(runtime_root)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    records = raw.values() if isinstance(raw, dict) else raw if isinstance(raw, list) else []
    configs: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        node_id = str(record.get("node_id") or "").strip()
        if node_id:
            configs[node_id] = dict(record)
    return configs


def ws_url_from_peer_record(record: dict[str, Any]) -> str:
    explicit = str(record.get("target_ws_url") or record.get("ws_url") or "").strip()
    if explicit:
        return explicit
    base_url = str(record.get("base_url") or "").strip().rstrip("/")
    if not base_url:
        return ""
    if base_url.startswith("https://"):
        return "wss://" + base_url[len("https://") :] + "/ws"
    if base_url.startswith("http://"):
        return "ws://" + base_url[len("http://") :] + "/ws"
    return base_url + "/ws"


def router_peer_config(runtime_root: Path, node_id: str) -> dict[str, Any] | None:
    configs = load_ws_router_peer_configs(runtime_root)
    if node_id in configs:
        return configs[node_id]
    peer = get_peer(runtime_root, node_id)
    if peer:
        return dict(peer)
    return None


def _matches_policy(value: str, allowed: list[Any]) -> bool:
    allowed_text = {str(item).strip() for item in allowed if str(item).strip()}
    return "*" in allowed_text or value in allowed_text


def authorize_inbound_kernel_message(
    runtime_root: Path,
    *,
    manifest: dict[str, Any],
    auth_context: dict[str, Any] | None,
    message: dict[str, Any],
) -> tuple[bool, str]:
    if not isinstance(message, dict):
        return False, "message_required"
    from_node = str(message_meta_get(message, "from_node", "")).strip()
    to_node = str(message_meta_get(message, "to_node", "")).strip()
    if not from_node or not to_node:
        return False, "missing_node_metadata"
    if to_node != str(manifest.get("node_id") or ""):
        return False, "not_addressed_to_this_node"
    if from_node == str(manifest.get("node_id") or ""):
        return False, "remote_transport_cannot_claim_local_node"
    auth_node = str((auth_context or {}).get("node_id") or "").strip()
    if auth_node and auth_node != from_node:
        return False, "authenticated_node_mismatch"

    config = load_ws_router_peer_configs(runtime_root).get(from_node)
    if config is None:
        peer = load_peers(runtime_root).get(from_node)
        if not isinstance(peer, dict) or not bool(peer.get("trusted")):
            return False, "untrusted_node"
        config = peer

    allowed_from_nodes = _as_list(config.get("accept_from_nodes") or config.get("trusted_from_nodes"))
    if allowed_from_nodes and not _matches_policy(from_node, allowed_from_nodes):
        return False, "from_node_not_allowed"
    allowed_to_services = _as_list(config.get("accept_to_services") or config.get("allowed_to_services"))
    if allowed_to_services and not _matches_policy(str(message.get("to", "")), allowed_to_services):
        return False, "recipient_not_allowed"
    allowed_message_types = _as_list(config.get("accept_message_types") or config.get("allowed_message_types"))
    if allowed_message_types and not _matches_policy(str(message.get("type", "")), allowed_message_types):
        return False, "message_type_not_allowed"
    return True, "trusted_ws_kernel_peer"


def mark_inbound_kernel_transport(message: dict[str, Any], *, peer_username: str) -> dict[str, Any]:
    marked = dict(message)
    meta = dict(marked.get("meta") if isinstance(marked.get("meta"), dict) else {})
    meta["ingress_transport"] = KERNEL_WS_TRANSPORT
    meta["ingress_authenticated_username"] = peer_username
    marked["meta"] = meta
    marked["ingress_transport"] = KERNEL_WS_TRANSPORT
    return marked


def is_ws_kernel_ingress(message: dict[str, Any]) -> bool:
    return str(message_meta_get(message, "ingress_transport", "")).strip() == KERNEL_WS_TRANSPORT


def _open_ws(url: str, *, timeout: float = 8.0, verify_tls: bool = False):
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in {"ws", "wss"}:
        raise OSError(f"unsupported_ws_scheme:{scheme}")
    host = parsed.hostname or ""
    port = parsed.port or (443 if scheme == "wss" else 80)
    path = parsed.path or "/ws"
    if parsed.query:
        path += "?" + parsed.query
    raw_sock = socket.create_connection((host, port), timeout=timeout)
    if scheme == "wss":
        ctx = ssl.create_default_context() if verify_tls else ssl._create_unverified_context()
        raw_sock = ctx.wrap_socket(raw_sock, server_hostname=host)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    headers = [
        f"GET {path} HTTP/1.1",
        f"Host: {host}:{port}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
        "",
        "",
    ]
    raw_sock.sendall("\r\n".join(headers).encode("ascii"))
    rfile = raw_sock.makefile("rb", buffering=0)
    wfile = raw_sock.makefile("wb", buffering=0)
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = rfile.read(1)
        if not chunk:
            raise OSError("websocket_upgrade_eof")
        response += chunk
        if len(response) > 8192:
            raise OSError("websocket_upgrade_response_too_large")
    status_line = response.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
    if " 101 " not in f" {status_line} ":
        raise OSError(f"websocket_upgrade_failed:{status_line}")
    return raw_sock, rfile, wfile


def forward_message_via_ws(
    runtime_root: Path,
    *,
    manifest: dict[str, Any],
    message: dict[str, Any],
) -> tuple[bool, str]:
    to_node = str(message_meta_get(message, "to_node", "")).strip()
    config = router_peer_config(runtime_root, to_node)
    if not config:
        return False, f"unknown_peer:{to_node}"
    ws_url = ws_url_from_peer_record(config)
    if not ws_url:
        return False, f"missing_ws_url:{to_node}"

    sock = None
    try:
        verify_tls = bool(config.get("tls_verify", False))
        sock, rfile, wfile = _open_ws(ws_url, verify_tls=verify_tls)
        auth_username = str(config.get("auth_username") or "").strip()
        auth_password = str(config.get("auth_password") or "").strip()
        if auth_username or auth_password:
            write_masked_text_frame(
                wfile,
                json.dumps(
                    {
                        "type": "auth",
                        "username": auth_username,
                        "password": auth_password,
                        "node_id": str(manifest.get("node_id") or ""),
                    },
                    ensure_ascii=False,
                ),
            )
            opcode, payload = read_frame(rfile) or (OP_CLOSE, b"")
            if opcode != OP_TEXT:
                return False, "auth_failed:no_text_response"
            auth_reply = json.loads(payload.decode("utf-8"))
            if not isinstance(auth_reply, dict) or auth_reply.get("type") != "auth_ok":
                return False, f"auth_failed:{auth_reply}"

        outbound = dict(message)
        message_set_meta(outbound, "transport", KERNEL_WS_TRANSPORT)
        write_masked_text_frame(
            wfile,
            json.dumps({"type": KERNEL_WS_MESSAGE_TYPE, "message": outbound}, ensure_ascii=False),
        )
        while True:
            frame = read_frame(rfile)
            if frame is None:
                return False, "connection_closed_before_ack"
            opcode, payload = frame
            if opcode == OP_PING:
                continue
            if opcode != OP_TEXT:
                continue
            reply = json.loads(payload.decode("utf-8"))
            if isinstance(reply, dict) and reply.get("type") == KERNEL_WS_ACCEPTED_TYPE:
                return bool(reply.get("accepted")), str(reply.get("detail") or ws_url)
            if isinstance(reply, dict) and reply.get("type") == "error":
                return False, str(reply.get("message") or "remote_error")
    except Exception as exc:
        return False, str(exc)
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
