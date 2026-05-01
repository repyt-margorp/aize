"""WebSocket peer connection handler for AIze-to-AIze sessions.

Protocol (JSON over WebSocket text frames):

  Client → Server
  ---------------
  {"type": "auth", "username": "...", "password": "...", "node_id": "..."}
  {"type": "list_open_sessions"}
  {"type": "join_session",  "username": "...", "session_id": "..."}
  {"type": "leave_session", "username": "...", "session_id": "..."}
  {"type": "message",       "username": "...", "session_id": "...", "text": "..."}
  {"type": "ping"}

  File protocol (requires prior join_session):
  {"type": "file_write",  "username": "...", "session_id": "...", "box": "inbox|outbox",
                          "filename": "...", "content_b64": "..."}
  {"type": "file_read",   "username": "...", "session_id": "...", "box": "inbox|outbox",
                          "filename": "..."}
  {"type": "file_list",   "username": "...", "session_id": "...", "box": "inbox|outbox"}
  {"type": "file_delete", "username": "...", "session_id": "...", "box": "inbox|outbox",
                          "filename": "..."}

  Server → Client
  ---------------
  {"type": "auth_ok",        "node_id": "...", "peer_id": "...", "run_id": "..."}
  {"type": "auth_error",     "message": "..."}
  {"type": "open_sessions",  "sessions": [...]}
  {"type": "session_joined", "username": "...", "session_id": "..."}
  {"type": "session_left",   "username": "...", "session_id": "..."}
  {"type": "session_event",  "username": "...", "session_id": "...", "entry": {...}}
  {"type": "message_accepted","username": "...", "session_id": "..."}
  {"type": "pong"}
  {"type": "error",          "message": "..."}

  File protocol responses:
  {"type": "file_write_ok",  "username": "...", "session_id": "...", "box": "...",
                             "filename": "...", "size": N}
  {"type": "file_read_result","username": "...", "session_id": "...", "box": "...",
                             "filename": "...", "size": N, "content_b64": "..."}
  {"type": "file_list_result","username": "...", "session_id": "...", "box": "...",
                             "files": [{"filename": "...", "size": N, "modified_at": "..."}]}
  {"type": "file_delete_ok", "username": "...", "session_id": "...", "box": "...",
                             "filename": "...", "deleted": true|false}
"""
from __future__ import annotations

import base64
import json
import queue
import threading
from pathlib import Path
from typing import Any, Callable

from kernel.identity import (
    client_auth_payload,
    ensure_node_identity,
    node_id_from_public_key_b64,
    normalize_public_key_b64,
    random_challenge,
    server_auth_payload,
    sign_payload,
    verify_signature,
)
from kernel.ws_transport import (
    KERNEL_WS_ACCEPTED_TYPE,
    KERNEL_WS_MESSAGE_TYPE,
    authorize_inbound_kernel_message,
    load_ws_router_peer_configs,
    mark_inbound_kernel_transport,
)
from runtime.persistent_state import (
    write_agent_file,
    read_agent_file,
    list_agent_files,
    delete_agent_file,
    check_agent_file_acl,
)
from runtime.ws_bridge import (
    OP_CLOSE,
    OP_PING,
    OP_TEXT,
    read_frame,
    write_close_frame,
    write_pong_frame,
    write_text_frame,
)
from wire.protocol import utc_ts


def handle_peer_connection(
    *,
    rfile: Any,
    wfile: Any,
    runtime_root: Path,
    manifest: dict[str, Any],
    self_service: dict[str, Any],
    process_id: str,
    log_path: Path,
    # Shared state / callbacks
    append_history: Callable,
    verify_user_password: Callable,
    list_peer_joinable_sessions: Callable,
    register_history_subscriber: Callable,
    unregister_history_subscriber: Callable,
    record_session_agent_contact: Callable,
    write_jsonl: Callable,
    send_router_control: Callable[[dict[str, Any]], bool] | None = None,
) -> None:
    """Run the WebSocket peer session loop until the connection closes."""
    _lock = threading.Lock()          # guards _subscriptions
    _write_lock = threading.Lock()    # guards wfile writes (main loop + event pumps)
    _closed = threading.Event()
    _auth_context: dict[str, Any] | None = None
    _pending_auth: dict[str, Any] | None = None
    # key → subscriber queue  (key = "username::session_id")
    _subscriptions: dict[str, queue.Queue[dict[str, Any]]] = {}

    # ------------------------------------------------------------------ helpers

    def _send(msg: dict[str, Any]) -> bool:
        try:
            with _write_lock:
                write_text_frame(wfile, json.dumps(msg, ensure_ascii=False))
            return True
        except Exception:
            _closed.set()
            return False

    def _event_pump(key: str, username: str, session_id: str, sub_q: "queue.Queue[dict]") -> None:
        """Forward history entries to the remote peer (runs in its own thread)."""
        while not _closed.is_set():
            try:
                entry = sub_q.get(timeout=1.0)
                _send({
                    "type": "session_event",
                    "username": username,
                    "session_id": session_id,
                    "entry": entry,
                })
            except queue.Empty:
                continue

    def _subscribe(username: str, session_id: str) -> None:
        key = f"{username}::{session_id}"
        with _lock:
            if key in _subscriptions:
                return
            sub_q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=500)
            _subscriptions[key] = sub_q
        register_history_subscriber(username=username, session_id=session_id, subscriber=sub_q)
        t = threading.Thread(
            target=_event_pump,
            args=(key, username, session_id, sub_q),
            daemon=True,
        )
        t.start()

    def _unsubscribe(username: str, session_id: str) -> None:
        key = f"{username}::{session_id}"
        with _lock:
            sub_q = _subscriptions.pop(key, None)
        if sub_q is not None:
            unregister_history_subscriber(username=username, session_id=session_id, subscriber=sub_q)

    def _unsubscribe_all() -> None:
        with _lock:
            pairs = list(_subscriptions.keys())
        for key in pairs:
            parts = key.split("::", 1)
            if len(parts) == 2:
                _unsubscribe(parts[0], parts[1])

    # ----------------------------------------------------------------- handlers

    def _public_key_trusted_for_node(node_id: str, public_key: str) -> bool:
        configs = load_ws_router_peer_configs(runtime_root)
        config = configs.get(node_id)
        if not config:
            return False
        expected = str(
            config.get("public_key")
            or config.get("peer_public_key")
            or config.get("public_key_der_b64")
            or ""
        ).strip()
        if not expected:
            return True
        try:
            return normalize_public_key_b64(expected) == normalize_public_key_b64(public_key)
        except Exception:
            return False

    def _handle_auth_init(msg: dict[str, Any]) -> None:
        nonlocal _pending_auth
        if str(msg.get("auth_type") or "").strip().lower() != "ed25519":
            _send({"type": "auth_error", "message": "unsupported_auth_type"})
            return
        client_node_id = str(msg.get("node_id") or "").strip()
        client_public_key = str(msg.get("public_key") or "").strip()
        client_challenge = str(msg.get("client_challenge") or "").strip()
        if not client_node_id or not client_public_key or not client_challenge:
            _send({"type": "auth_error", "message": "ed25519_auth_init_fields_required"})
            return
        try:
            client_public_key = normalize_public_key_b64(client_public_key)
            derived_node_id = node_id_from_public_key_b64(client_public_key)
        except Exception:
            _send({"type": "auth_error", "message": "invalid_public_key"})
            return
        if client_node_id != derived_node_id:
            _send({"type": "auth_error", "message": "node_id_public_key_mismatch"})
            return
        if not _public_key_trusted_for_node(client_node_id, client_public_key):
            _send({"type": "auth_error", "message": "untrusted_public_key"})
            return

        server_identity = ensure_node_identity(runtime_root)
        server_node_id = str(server_identity["node_id"])
        server_public_key = str(server_identity["public_key"])
        server_challenge = random_challenge()
        payload = server_auth_payload(
            client_node_id=client_node_id,
            client_public_key=client_public_key,
            client_challenge=client_challenge,
            server_node_id=server_node_id,
            server_public_key=server_public_key,
            server_challenge=server_challenge,
        )
        server_signature = sign_payload(runtime_root, payload)
        _pending_auth = {
            "client_node_id": client_node_id,
            "client_public_key": client_public_key,
            "client_challenge": client_challenge,
            "server_node_id": server_node_id,
            "server_public_key": server_public_key,
            "server_challenge": server_challenge,
        }
        _send({
            "type": "auth_challenge",
            "auth_type": "ed25519",
            "node_id": server_node_id,
            "public_key": server_public_key,
            "server_challenge": server_challenge,
            "server_signature": server_signature,
        })

    def _handle_ed25519_auth(msg: dict[str, Any]) -> None:
        nonlocal _auth_context, _pending_auth
        if not _pending_auth:
            _send({"type": "auth_error", "message": "auth_init_required"})
            return
        client_node_id = str(_pending_auth.get("client_node_id") or "")
        client_public_key = str(_pending_auth.get("client_public_key") or "")
        signature = str(msg.get("signature") or "").strip()
        if not signature:
            _send({"type": "auth_error", "message": "signature_required"})
            return
        payload = client_auth_payload(
            client_node_id=client_node_id,
            client_public_key=client_public_key,
            client_challenge=str(_pending_auth.get("client_challenge") or ""),
            server_node_id=str(_pending_auth.get("server_node_id") or ""),
            server_public_key=str(_pending_auth.get("server_public_key") or ""),
            server_challenge=str(_pending_auth.get("server_challenge") or ""),
        )
        if not verify_signature(client_public_key, payload, signature):
            _send({"type": "auth_error", "message": "invalid_signature"})
            return
        username = str(msg.get("username") or client_node_id).strip()
        server_public_key = str(_pending_auth.get("server_public_key") or "")
        _auth_context = {
            "username": username,
            "node_id": client_node_id,
            "public_key": client_public_key,
            "auth_type": "ed25519",
        }
        _pending_auth = None
        peer_meta = manifest.get("peer") or {}
        write_jsonl(log_path, {
            "type": "ws_peer.auth_ok",
            "ts": utc_ts(),
            "service_id": self_service["service_id"],
            "peer_username": username,
            "peer_node_id": client_node_id,
            "auth_type": "ed25519",
        })
        _send({
            "type": "auth_ok",
            "auth_type": "ed25519",
            "node_id": str(manifest.get("node_id") or ""),
            "public_key": server_public_key,
            "peer_id": str(peer_meta.get("peer_id") or ""),
            "run_id": str(manifest.get("run_id") or ""),
        })

    def _handle_auth(msg: dict[str, Any]) -> None:
        nonlocal _auth_context
        if str(msg.get("auth_type") or "").strip().lower() == "ed25519":
            _handle_ed25519_auth(msg)
            return
        username = str(msg.get("username", "")).strip()
        password = str(msg.get("password", "")).strip()
        if not username or not password:
            _send({"type": "auth_error", "message": "username_and_password_required"})
            return
        ok = verify_user_password(runtime_root, username=username, password=password)
        if not ok:
            _send({"type": "auth_error", "message": "invalid_credentials"})
            return
        _auth_context = {
            "username": username,
            "node_id": str(msg.get("node_id", "")).strip(),
        }
        peer_meta = manifest.get("peer") or {}
        write_jsonl(log_path, {
            "type": "ws_peer.auth_ok",
            "ts": utc_ts(),
            "service_id": self_service["service_id"],
            "peer_username": username,
        })
        _send({
            "type": "auth_ok",
            "node_id": str(manifest.get("node_id") or ""),
            "peer_id": str(peer_meta.get("peer_id") or ""),
            "run_id": str(manifest.get("run_id") or ""),
        })

    def _handle_list_open_sessions(_msg: dict[str, Any]) -> None:
        if _auth_context is None:
            _send({"type": "error", "message": "not_authenticated"})
            return
        sessions = list_peer_joinable_sessions(runtime_root)
        _send({"type": "open_sessions", "sessions": sessions})

    def _handle_join_session(msg: dict[str, Any]) -> None:
        if _auth_context is None:
            _send({"type": "error", "message": "not_authenticated"})
            return
        username = str(msg.get("username", "")).strip()
        session_id = str(msg.get("session_id", "")).strip()
        if not username or not session_id:
            _send({"type": "error", "message": "username_and_session_id_required"})
            return
        # Verify the session is marked as peer-joinable
        open_sessions = list_peer_joinable_sessions(runtime_root)
        joinable = any(
            str(s.get("username", "")) == username and str(s.get("session_id", "")) == session_id
            for s in open_sessions
        )
        if not joinable:
            _send({"type": "error", "message": "session_not_joinable"})
            return
        _subscribe(username, session_id)

        # Register the external AIze peer as a welcomed agent in this session
        peer_username = str(_auth_context.get("username", "peer")).strip()
        # Use the node_id the peer advertised in its auth message; fall back to username.
        peer_node_id = str(_auth_context.get("node_id") or "").strip()
        # Derive a stable virtual service_id for this peer based on its node
        peer_service_id = f"ws-peer-{peer_node_id or peer_username}"
        record_session_agent_contact(
            runtime_root,
            username=username,
            session_id=session_id,
            service_id=peer_service_id,
            agent_id=f"{peer_service_id}@@{session_id}",
            provider="ws_peer",
        )

        write_jsonl(log_path, {
            "type": "ws_peer.session_joined",
            "ts": utc_ts(),
            "service_id": self_service["service_id"],
            "peer_username": peer_username,
            "peer_service_id": peer_service_id,
            "target_username": username,
            "session_id": session_id,
        })
        _send({
            "type": "session_joined",
            "username": username,
            "session_id": session_id,
            "peer_service_id": peer_service_id,
        })

    def _handle_leave_session(msg: dict[str, Any]) -> None:
        username = str(msg.get("username", "")).strip()
        session_id = str(msg.get("session_id", "")).strip()
        _unsubscribe(username, session_id)
        _send({"type": "session_left", "username": username, "session_id": session_id})

    def _handle_message(msg: dict[str, Any]) -> None:
        """Record the external AIze's text as an agent turn (not a user prompt).

        The sequence written to history is:
          1. event  direction="event"  event_type="agent.turn_started"
          2. reply  direction="in"     from=peer_service_id
          3. event  direction="event"  event_type="turn.completed"

        This makes the peer's contribution visible in the timeline as an agent
        turn that the GoalManager can evaluate like any local agent's turn.
        No local LLM is invoked — the peer itself IS the responding agent.
        """
        if _auth_context is None:
            _send({"type": "error", "message": "not_authenticated"})
            return
        username = str(msg.get("username", "")).strip()
        session_id = str(msg.get("session_id", "")).strip()
        text = str(msg.get("text", "")).strip()
        if not username or not session_id:
            _send({"type": "error", "message": "username_and_session_id_required"})
            return
        if not text:
            _send({"type": "error", "message": "text_required"})
            return
        key = f"{username}::{session_id}"
        with _lock:
            if key not in _subscriptions:
                _send({"type": "error", "message": "not_joined_to_session"})
                return

        peer_label = str(_auth_context.get("username", "peer")).strip()
        peer_node_id = str(_auth_context.get("node_id") or "").strip()
        peer_service_id = f"ws-peer-{peer_node_id or peer_label}"
        now = utc_ts()

        # 1. agent.turn_started event
        append_history(
            username,
            session_id,
            {
                "direction": "event",
                "ts": now,
                "service_id": peer_service_id,
                "session_id": session_id,
                "event_type": "agent.turn_started",
                "text": "response started",
                "event": {"type": "agent.turn_started", "service_id": peer_service_id},
            },
        )

        # 2. Agent reply (direction: "in")
        append_history(
            username,
            session_id,
            {
                "direction": "in",
                "ts": now,
                "from": peer_service_id,
                "session_id": session_id,
                "text": text,
                "peer_source": peer_label,
            },
        )

        # 3. turn.completed event
        append_history(
            username,
            session_id,
            {
                "direction": "event",
                "ts": now,
                "service_id": peer_service_id,
                "session_id": session_id,
                "event_type": "turn.completed",
                "text": "Turn completed",
                "event": {
                    "type": "turn.completed",
                    "status": "success",
                    "provider": "ws_peer",
                    "peer_service_id": peer_service_id,
                },
            },
        )

        write_jsonl(log_path, {
            "type": "ws_peer.agent_turn_recorded",
            "ts": now,
            "service_id": self_service["service_id"],
            "peer_service_id": peer_service_id,
            "peer_username": peer_label,
            "target_username": username,
            "session_id": session_id,
        })
        _send({"type": "message_accepted", "username": username, "session_id": session_id})

    def _handle_kernel_message(msg: dict[str, Any]) -> None:
        if _auth_context is None:
            _send({"type": "error", "message": "not_authenticated"})
            return
        if send_router_control is None:
            _send({"type": "error", "message": "kernel_router_unavailable"})
            return
        message = msg.get("message")
        if not isinstance(message, dict):
            _send({"type": "error", "message": "message_required"})
            return
        allowed, detail = authorize_inbound_kernel_message(
            runtime_root,
            manifest=manifest,
            auth_context=_auth_context,
            message=message,
        )
        if not allowed:
            write_jsonl(log_path, {
                "type": "ws_kernel.message_rejected",
                "ts": utc_ts(),
                "reason": detail,
                "peer_username": str(_auth_context.get("username", "")),
                "peer_node_id": str(_auth_context.get("node_id", "")),
            })
            _send({"type": KERNEL_WS_ACCEPTED_TYPE, "accepted": False, "detail": detail})
            return
        peer_username = str(_auth_context.get("username", "")).strip()
        marked = mark_inbound_kernel_transport(message, peer_username=peer_username)
        accepted = bool(send_router_control(marked))
        write_jsonl(log_path, {
            "type": "ws_kernel.message_received",
            "ts": utc_ts(),
            "accepted": accepted,
            "detail": detail if accepted else "router_control_injection_failed",
            "from_node": str(marked.get("from_node") or (marked.get("meta") or {}).get("from_node") or ""),
            "to_node": str(marked.get("to_node") or (marked.get("meta") or {}).get("to_node") or ""),
            "from": str(marked.get("from", "")),
            "to": str(marked.get("to", "")),
        })
        _send({
            "type": KERNEL_WS_ACCEPTED_TYPE,
            "accepted": accepted,
            "detail": detail if accepted else "router_control_injection_failed",
        })

    # ----------------------------------------------------------------- file handlers

    def _peer_service_id_for_file(msg: dict[str, Any]) -> str | None:
        """Return the peer's virtual service_id, or None if not authenticated."""
        if _auth_context is None:
            return None
        peer_node_id = str(_auth_context.get("node_id") or "").strip()
        peer_label = str(_auth_context.get("username", "peer")).strip()
        return f"ws-peer-{peer_node_id or peer_label}"

    def _require_joined_session(msg: dict[str, Any]) -> tuple[str, str, str, str] | None:
        """Validate auth + session membership.

        Returns (username, session_id, caller_agent_id, dir_agent_id) or None.
        ``caller_agent_id`` is the peer's own identity.
        ``dir_agent_id`` is the target directory: the message's ``dir_agent_id``
        field if provided, otherwise the caller's own agent_id.
        """
        if _auth_context is None:
            _send({"type": "error", "message": "not_authenticated"})
            return None
        username = str(msg.get("username", "")).strip()
        session_id = str(msg.get("session_id", "")).strip()
        if not username or not session_id:
            _send({"type": "error", "message": "username_and_session_id_required"})
            return None
        key = f"{username}::{session_id}"
        with _lock:
            if key not in _subscriptions:
                _send({"type": "error", "message": "not_joined_to_session"})
                return None
        peer_service_id = _peer_service_id_for_file(msg)
        caller_agent_id = f"{peer_service_id}@@{session_id}"
        dir_agent_id_override = str(msg.get("dir_agent_id") or "").strip()
        dir_agent_id = dir_agent_id_override if dir_agent_id_override else caller_agent_id
        return username, session_id, caller_agent_id, dir_agent_id

    def _handle_file_write(msg: dict[str, Any]) -> None:
        result = _require_joined_session(msg)
        if result is None:
            return
        username, session_id, caller_agent_id, dir_agent_id = result
        box = str(msg.get("box", "")).strip().lower()
        filename = str(msg.get("filename", "")).strip()
        content_b64 = str(msg.get("content_b64", "")).strip()
        if box not in {"inbox", "outbox"}:
            _send({"type": "error", "message": "box_must_be_inbox_or_outbox"})
            return
        if not filename:
            _send({"type": "error", "message": "filename_required"})
            return
        if not content_b64:
            _send({"type": "error", "message": "content_b64_required"})
            return
        if not check_agent_file_acl(runtime_root, username=username, session_id=session_id,
                                     dir_agent_id=dir_agent_id, caller_agent_id=caller_agent_id,
                                     permission="write"):
            _send({"type": "error", "message": "access_denied"})
            return
        try:
            content = base64.b64decode(content_b64)
        except Exception:
            _send({"type": "error", "message": "invalid_base64"})
            return
        ok = write_agent_file(
            runtime_root,
            username=username,
            session_id=session_id,
            agent_id=dir_agent_id,
            box=box,
            filename=filename,
            content=content,
        )
        if not ok:
            _send({"type": "error", "message": "file_write_failed"})
            return
        _send({
            "type": "file_write_ok",
            "username": username,
            "session_id": session_id,
            "dir_agent_id": dir_agent_id,
            "box": box,
            "filename": filename,
            "size": len(content),
        })

    def _handle_file_read(msg: dict[str, Any]) -> None:
        result = _require_joined_session(msg)
        if result is None:
            return
        username, session_id, caller_agent_id, dir_agent_id = result
        box = str(msg.get("box", "")).strip().lower()
        filename = str(msg.get("filename", "")).strip()
        if box not in {"inbox", "outbox"}:
            _send({"type": "error", "message": "box_must_be_inbox_or_outbox"})
            return
        if not filename:
            _send({"type": "error", "message": "filename_required"})
            return
        if not check_agent_file_acl(runtime_root, username=username, session_id=session_id,
                                     dir_agent_id=dir_agent_id, caller_agent_id=caller_agent_id,
                                     permission="read"):
            _send({"type": "error", "message": "access_denied"})
            return
        content = read_agent_file(
            runtime_root,
            username=username,
            session_id=session_id,
            agent_id=dir_agent_id,
            box=box,
            filename=filename,
        )
        if content is None:
            _send({"type": "error", "message": "file_not_found"})
            return
        _send({
            "type": "file_read_result",
            "username": username,
            "session_id": session_id,
            "dir_agent_id": dir_agent_id,
            "box": box,
            "filename": filename,
            "size": len(content),
            "content_b64": base64.b64encode(content).decode("ascii"),
        })

    def _handle_file_list(msg: dict[str, Any]) -> None:
        result = _require_joined_session(msg)
        if result is None:
            return
        username, session_id, caller_agent_id, dir_agent_id = result
        box = str(msg.get("box", "")).strip().lower()
        if box not in {"inbox", "outbox"}:
            _send({"type": "error", "message": "box_must_be_inbox_or_outbox"})
            return
        if not check_agent_file_acl(runtime_root, username=username, session_id=session_id,
                                     dir_agent_id=dir_agent_id, caller_agent_id=caller_agent_id,
                                     permission="read"):
            _send({"type": "error", "message": "access_denied"})
            return
        files = list_agent_files(
            runtime_root,
            username=username,
            session_id=session_id,
            agent_id=dir_agent_id,
            box=box,
        )
        _send({
            "type": "file_list_result",
            "username": username,
            "session_id": session_id,
            "dir_agent_id": dir_agent_id,
            "box": box,
            "files": files,
        })

    def _handle_file_delete(msg: dict[str, Any]) -> None:
        result = _require_joined_session(msg)
        if result is None:
            return
        username, session_id, caller_agent_id, dir_agent_id = result
        box = str(msg.get("box", "")).strip().lower()
        filename = str(msg.get("filename", "")).strip()
        if box not in {"inbox", "outbox"} or not filename:
            _send({"type": "error", "message": "box_and_filename_required"})
            return
        if not check_agent_file_acl(runtime_root, username=username, session_id=session_id,
                                     dir_agent_id=dir_agent_id, caller_agent_id=caller_agent_id,
                                     permission="write"):
            _send({"type": "error", "message": "access_denied"})
            return
        deleted = delete_agent_file(
            runtime_root,
            username=username,
            session_id=session_id,
            agent_id=dir_agent_id,
            box=box,
            filename=filename,
        )
        _send({
            "type": "file_delete_ok",
            "username": username,
            "session_id": session_id,
            "dir_agent_id": dir_agent_id,
            "box": box,
            "filename": filename,
            "deleted": deleted,
        })

    # ----------------------------------------------------------------- main loop

    _DISPATCH: dict[str, Callable[[dict[str, Any]], None]] = {
        "auth_init": _handle_auth_init,
        "auth": _handle_auth,
        "list_open_sessions": _handle_list_open_sessions,
        "join_session": _handle_join_session,
        "leave_session": _handle_leave_session,
        "message": _handle_message,
        KERNEL_WS_MESSAGE_TYPE: _handle_kernel_message,
        "file_write": _handle_file_write,
        "file_read": _handle_file_read,
        "file_list": _handle_file_list,
        "file_delete": _handle_file_delete,
    }

    try:
        while not _closed.is_set():
            result = read_frame(rfile)
            if result is None:
                break
            opcode, payload = result

            if opcode == OP_CLOSE:
                try:
                    write_close_frame(wfile)
                except Exception:
                    pass
                break

            if opcode == OP_PING:
                try:
                    write_pong_frame(wfile, payload)
                except Exception:
                    pass
                continue

            if opcode != OP_TEXT:
                # Ignore binary / continuation frames
                continue

            try:
                msg = json.loads(payload.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                _send({"type": "error", "message": "invalid_json"})
                continue

            if not isinstance(msg, dict):
                _send({"type": "error", "message": "expected_object"})
                continue

            msg_type = str(msg.get("type", "")).strip()

            if msg_type == "ping":
                _send({"type": "pong"})
                continue

            handler = _DISPATCH.get(msg_type)
            if handler is None:
                _send({"type": "error", "message": f"unknown_type:{msg_type}"})
            else:
                try:
                    handler(msg)
                except Exception as exc:
                    _send({"type": "error", "message": f"handler_error:{exc}"})

    finally:
        _closed.set()
        _unsubscribe_all()
