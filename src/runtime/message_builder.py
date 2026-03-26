from __future__ import annotations

import html
import json
import uuid
from pathlib import Path
from typing import Any

from runtime.persistent_state import release_session_service
from wire.protocol import (
    load_text_object,
    make_message,
    message_meta_get,
    message_set_meta,
    store_text_object,
    utc_ts,
)


def maybe_release_session_provider(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    talk: dict[str, Any] | None,
) -> str | None:
    if not isinstance(talk, dict):
        return None
    goal_active = bool(talk.get("goal_active", False))
    goal_completed = bool(talk.get("goal_completed", False))
    goal_progress_state = str(
        talk.get("goal_progress_state", "complete" if goal_completed else "in_progress")
    ).strip().lower()
    if goal_completed or not goal_active or goal_progress_state == "complete":
        return release_session_service(runtime_root, username=username, session_id=session_id)
    return None


def load_manifest(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def inline_limit_bytes(manifest: dict) -> int:
    settings = manifest.get("settings", {})
    return int(settings.get("inline_payload_max_bytes", 4096))


def make_process_id(service_id: str) -> str:
    return f"proc-{service_id}-{uuid.uuid4().hex[:8]}"


def resolve_payload_text(runtime_root: Path, message: dict) -> str:
    payload = message.get("payload")
    if isinstance(payload, dict) and "text" in payload:
        return str(payload["text"])
    payload_ref = message.get("payload_ref")
    if payload_ref:
        return load_text_object(runtime_root / "objects", str(payload_ref))
    raise RuntimeError("message has neither payload.text nor payload_ref")


def resolve_event_entry(runtime_root: Path, message: dict) -> dict[str, Any] | None:
    payload = message.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("entry"), dict):
        return dict(payload["entry"])
    payload_ref = message.get("payload_ref")
    if payload_ref:
        try:
            text = load_text_object(runtime_root / "objects", str(payload_ref))
            record = json.loads(text)
        except Exception:
            return None
        if isinstance(record, dict) and isinstance(record.get("entry"), dict):
            return dict(record["entry"])
    return None


def build_outgoing_message(
    *,
    runtime_root: Path,
    manifest: dict,
    from_node_id: str,
    from_service_id: str,
    to_node_id: str,
    to_service_id: str,
    process_id: str,
    run_id: str,
    text: str,
    username: str | None = None,
    session_id: str | None = None,
    auth_context: dict[str, Any] | None = None,
) -> dict:
    text_bytes = text.encode("utf-8")
    if len(text_bytes) <= inline_limit_bytes(manifest):
        message = make_message(
            from_node_id=from_node_id,
            from_service_id=from_service_id,
            to_node_id=to_node_id,
            to_service_id=to_service_id,
            message_type="prompt",
            payload={"text": text},
            run_id=run_id,
        )
        message_set_meta(message, "process_id", process_id)
        if username and session_id:
            message_set_meta(message, "conversation", {"username": username, "session_id": session_id})
        if auth_context:
            message_set_meta(message, "auth", auth_context)
        return message

    object_id = store_text_object(runtime_root / "objects", text)
    message = make_message(
        from_node_id=from_node_id,
        from_service_id=from_service_id,
        to_node_id=to_node_id,
        to_service_id=to_service_id,
        message_type="prompt",
        payload_ref=object_id,
        run_id=run_id,
    )
    message_set_meta(message, "process_id", process_id)
    if username and session_id:
        message_set_meta(message, "conversation", {"username": username, "session_id": session_id})
    if auth_context:
        message_set_meta(message, "auth", auth_context)
    return message


def build_outgoing_event_message(
    *,
    runtime_root: Path,
    manifest: dict,
    from_node_id: str,
    from_service_id: str,
    to_node_id: str,
    to_service_id: str,
    process_id: str,
    run_id: str,
    entry: dict[str, Any],
    username: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    payload_record = {"entry": entry}
    payload_text = json.dumps(payload_record, ensure_ascii=False)
    force_ref = to_service_id == "service-http-001"
    if not force_ref and len(payload_text.encode("utf-8")) <= inline_limit_bytes(manifest):
        message = make_message(
            from_node_id=from_node_id,
            from_service_id=from_service_id,
            to_node_id=to_node_id,
            to_service_id=to_service_id,
            message_type="event",
            payload=payload_record,
            run_id=run_id,
        )
    else:
        object_id = store_text_object(runtime_root / "objects", payload_text, kind="json")
        message = make_message(
            from_node_id=from_node_id,
            from_service_id=from_service_id,
            to_node_id=to_node_id,
            to_service_id=to_service_id,
            message_type="event",
            payload_ref=object_id,
            run_id=run_id,
        )
    message_set_meta(message, "process_id", process_id)
    if username and session_id:
        message_set_meta(message, "conversation", {"username": username, "session_id": session_id})
    return message


def resolve_conversation_scope(message: dict[str, Any]) -> tuple[str | None, str | None]:
    conversation = message_meta_get(message, "conversation")
    if isinstance(conversation, dict):
        username = conversation.get("username")
        session_id = conversation.get("session_id") or conversation.get("session_id")
        if isinstance(username, str) and isinstance(session_id, str):
            return username, session_id
    payload = message.get("payload")
    if isinstance(payload, dict):
        username = payload.get("username")
        session_id = payload.get("session_id") or payload.get("session_id")
        if isinstance(username, str) and isinstance(session_id, str):
            return username, session_id
    return None, None


def session_payload(session: dict[str, Any]) -> dict[str, Any]:
    session_id = str(session.get("session_id") or session.get("session_id") or "")
    if not session_id:
        return dict(session)
    return {**session, "session_id": session_id, "session_id": session_id}


def build_aize_input_batch_xml(
    *,
    sender_display_name: str,
    username: str,
    session_id: str,
    inputs: list[dict[str, Any]],
    instruction: str,
) -> str:
    lines = [
        "<aize_input_batch>",
        f"  <source>{html.escape(sender_display_name)}</source>",
        f"  <conversation username=\"{html.escape(username)}\" session_id=\"{html.escape(session_id)}\" />",
        "  <inputs>",
    ]
    for index, item in enumerate(inputs, start=1):
        kind = html.escape(str(item.get("kind", "user_message")))
        role = html.escape(str(item.get("role", "user")))
        text = html.escape(str(item.get("text", "")))
        date_attr = ""
        submitted_by_attr = ""
        raw_date = item.get("date")
        if isinstance(raw_date, str) and raw_date.strip():
            date_attr = f' date="{html.escape(raw_date.strip(), quote=True)}"'
        raw_submitted_by_username = item.get("submitted_by_username")
        if isinstance(raw_submitted_by_username, str) and raw_submitted_by_username.strip():
            submitted_by_attr = (
                f' submitted_by_username="{html.escape(raw_submitted_by_username.strip(), quote=True)}"'
            )
        lines.extend(
            [
                f"    <input index=\"{index}\" kind=\"{kind}\"{date_attr}{submitted_by_attr}>",
                f"      <role>{role}</role>",
                f"      <text>{text}</text>",
                "    </input>",
            ]
        )
    lines.extend(
        [
            "  </inputs>",
            f"  <instruction>{html.escape(instruction)}</instruction>",
            "</aize_input_batch>",
        ]
    )
    return "\n".join(lines)


def make_dispatch_pending_message(
    *,
    manifest: dict[str, Any],
    from_service_id: str,
    to_service_id: str,
    process_id: str,
    run_id: str,
    username: str,
    session_id: str,
    auth_context: dict[str, Any] | None,
    reason: str,
    reply_to_service_id: str = "service-http-001",
    session_agent_id: str | None = None,
) -> dict[str, Any]:
    message = make_message(
        from_node_id=manifest["node_id"],
        from_service_id=from_service_id,
        to_node_id=manifest["node_id"],
        to_service_id=to_service_id,
        message_type="dispatch_pending",
        payload={"reason": reason},
        run_id=run_id,
    )
    message_set_meta(message, "process_id", process_id)
    message_set_meta(message, "conversation", {"username": username, "session_id": session_id})
    message_set_meta(message, "reply_to_service_id", reply_to_service_id)
    if isinstance(session_agent_id, str) and session_agent_id.strip():
        message_set_meta(message, "session_agent_id", session_agent_id.strip())
    if isinstance(auth_context, dict):
        message_set_meta(message, "auth", dict(auth_context))
    return message


def make_aize_pending_input(
    *,
    kind: str,
    role: str,
    text: str,
    date: str | None = None,
    submitted_by_username: str | None = None,
) -> dict[str, str]:
    date_value = date.strip() if isinstance(date, str) and date.strip() else utc_ts()
    entry = {
        "kind": kind,
        "role": role,
        "text": text,
        "date": date_value,
    }
    if isinstance(submitted_by_username, str) and submitted_by_username.strip():
        entry["submitted_by_username"] = submitted_by_username.strip()
    return entry


def batch_has_input_kind(batch_xml: str, kind: str) -> bool:
    quoted = html.escape(kind, quote=True)
    return f'kind="{quoted}"' in batch_xml or f"kind='{quoted}'" in batch_xml


def dispatch_pending_opens_visible_turn(message: dict[str, Any], incoming_text: str) -> bool:
    if message.get("type") != "dispatch_pending":
        return False
    if batch_has_input_kind(incoming_text, "user_message"):
        return True
    payload = message.get("payload")
    reason = ""
    if isinstance(payload, dict):
        raw_reason = payload.get("reason")
        if isinstance(raw_reason, str):
            reason = raw_reason.strip().lower()
    return reason not in {
        "goal_feedback",
        "goal_manager_review",
        "turn_completed",
        "child_session_created",
        "child_session_completed",
    }
