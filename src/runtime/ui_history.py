from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from runtime.event_log import summarize_provider_event
from runtime.goal_persist import goal_audit_history_text
from runtime.message_builder import resolve_event_entry, resolve_payload_text
from runtime.persistent_state_pkg import (
    get_history as get_user_history,
    get_session_service,
    list_session_agent_contacts,
    read_jsonl,
    session_goal_manager_reviews_path,
    session_services_dir,
)
from runtime.persistent_state_pkg.history import sanitize_history_entry
from runtime.service_control import extract_assistant_text_lenient


def _runtime_logs_dir(runtime_root: Path) -> Path:
    return runtime_root / "logs"


def _candidate_service_ids(runtime_root: Path, *, username: str, session_id: str) -> list[str]:
    service_ids: list[str] = ["service-http-001"]
    bound_service_id = str(get_session_service(runtime_root, username=username, session_id=session_id) or "").strip()
    if bound_service_id:
        service_ids.append(bound_service_id)
    for item in list_session_agent_contacts(runtime_root, username=username, session_id=session_id):
        service_id = str(item.get("service_id") or "").strip()
        if service_id:
            service_ids.append(service_id)
    services_dir = session_services_dir(runtime_root, username=username, session_id=session_id)
    if services_dir.exists():
        for path in sorted(services_dir.glob("*.json")):
            name = path.name
            if name.endswith(".audit.json"):
                continue
            service_id = path.stem.strip()
            if service_id:
                service_ids.append(service_id)
    deduped: list[str] = []
    seen: set[str] = set()
    for service_id in service_ids:
        if service_id in seen:
            continue
        seen.add(service_id)
        deduped.append(service_id)
    return deduped


def _session_agent_role_map(runtime_root: Path, *, username: str, session_id: str) -> dict[str, set[str]]:
    roles: dict[str, set[str]] = {}
    for item in list_session_agent_contacts(runtime_root, username=username, session_id=session_id):
        service_id = str(item.get("service_id") or "").strip()
        if not service_id:
            continue
        join_role = str(item.get("join_role") or "").strip().lower()
        if join_role:
            roles.setdefault(service_id, set()).add(join_role)
    return roles


def _record_session_slot(record: dict[str, Any]) -> str:
    top_level_slot = str(record.get("session_slot") or record.get("provider_session_slot") or "").strip().lower()
    if top_level_slot:
        return top_level_slot
    event = record.get("event")
    if isinstance(event, dict):
        return str(event.get("session_slot") or event.get("provider_session_slot") or "").strip().lower()
    return ""


def _record_has_user_visible_provider_text(record: dict[str, Any]) -> bool:
    provider_event = record.get("provider_event")
    if not isinstance(provider_event, dict):
        provider_event = record.get("event")
    if not isinstance(provider_event, dict):
        return False
    provider_type = str(provider_event.get("type") or "").strip()
    if provider_type == "agent_message.delta":
        return bool(str(provider_event.get("delta") or "").strip())
    if provider_type != "item.completed":
        return False
    item = provider_event.get("item") if isinstance(provider_event.get("item"), dict) else {}
    return str(item.get("type") or "").strip() == "agent_message" and bool(str(item.get("text") or "").strip())


def _timeline_entry_is_ui_relevant(entry: dict[str, Any]) -> bool:
    direction = str(entry.get("direction") or "").strip().lower()
    if direction in {"out", "user"}:
        return True
    if direction == "in":
        source = str(entry.get("from") or "").strip()
        if not source.startswith("service-"):
            return True
        provider = str(entry.get("provider") or "").strip().lower()
        if provider == "communication_router":
            return True
        event = entry.get("event") if isinstance(entry.get("event"), dict) else {}
        return str(event.get("provider") or "").strip().lower() == "communication_router"
    if direction == "event":
        event_type = str(entry.get("event_type") or "").strip()
        event = entry.get("event") if isinstance(entry.get("event"), dict) else {}
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if event_type == "item.completed" and str(item.get("type") or "") == "agent_message":
            return bool(str(item.get("text") or entry.get("text") or "").strip())
        if event_type == "agent_message.delta":
            return bool(str(event.get("delta") or entry.get("text") or "").strip())
    return False


def _scope_matches(record: dict[str, Any], *, username: str, session_id: str) -> bool:
    scope = record.get("scope")
    if isinstance(scope, dict):
        if str(scope.get("username") or "").strip() == username and str(scope.get("session_id") or "").strip() == session_id:
            return True
    message = record.get("message")
    if isinstance(message, dict):
        meta = message.get("meta")
        if isinstance(meta, dict):
            conversation = meta.get("conversation")
            if isinstance(conversation, dict):
                if (
                    str(conversation.get("username") or "").strip() == username
                    and str(conversation.get("session_id") or "").strip() == session_id
                ):
                    return True
        conversation = message.get("conversation")
        if isinstance(conversation, dict):
            if (
                str(conversation.get("username") or "").strip() == username
                and str(conversation.get("session_id") or "").strip() == session_id
            ):
                return True
    return False


def _entry_key(entry: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    return (
        str(entry.get("ts") or ""),
        str(entry.get("direction") or ""),
        str(entry.get("event_type") or ""),
        str(entry.get("from") or ""),
        str(entry.get("service_id") or ""),
        str(entry.get("text") or "")[:200],
    )


def _entry_service_id(entry: dict[str, Any]) -> str:
    return str(entry.get("service_id") or entry.get("from") or "").strip()


def _entry_visible_text(entry: dict[str, Any]) -> str:
    raw_text = str(entry.get("text") or "").strip()
    parsed_text = extract_assistant_text_lenient(raw_text)
    if parsed_text:
        return parsed_text
    event = entry.get("event") if isinstance(entry.get("event"), dict) else {}
    item = event.get("item") if isinstance(event.get("item"), dict) else {}
    provider_event = event.get("provider_event") if isinstance(event.get("provider_event"), dict) else {}
    provider_item = provider_event.get("item") if isinstance(provider_event.get("item"), dict) else {}
    for value in (
        item.get("text"),
        provider_item.get("text"),
        event.get("delta"),
        provider_event.get("delta"),
        raw_text,
    ):
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _collapse_duplicate_interactive_replies(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    collapsed: list[dict[str, Any]] = []
    seen_replies_since_user: set[tuple[str, str]] = set()
    for entry in entries:
        direction = str(entry.get("direction") or "").strip().lower()
        if direction in {"out", "user", "session_input"}:
            seen_replies_since_user.clear()
            collapsed.append(entry)
            continue
        if direction != "in":
            collapsed.append(entry)
            continue
        service_id = _entry_service_id(entry)
        visible_text = _entry_visible_text(entry)
        if not service_id or not visible_text:
            collapsed.append(entry)
            continue
        key = (service_id, visible_text)
        if key in seen_replies_since_user:
            continue
        seen_replies_since_user.add(key)
        collapsed.append(entry)
    return collapsed


def _interactive_reply_ts(entry: dict[str, Any]) -> datetime | None:
    raw_ts = str(entry.get("ts") or "").strip()
    if not raw_ts:
        return None
    try:
        return datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_duplicate_final_interactive_output(entry: dict[str, Any]) -> bool:
    direction = str(entry.get("direction") or "").strip().lower()
    if direction == "agent":
        return True
    if direction != "event":
        return False
    event_type = str(entry.get("event_type") or "").strip()
    if event_type not in {"item.completed", "service.goal_manager_compact_provider_event.item.completed"}:
        return False
    event = entry.get("event") if isinstance(entry.get("event"), dict) else {}
    item = event.get("item") if isinstance(event.get("item"), dict) else {}
    provider_event = event.get("provider_event") if isinstance(event.get("provider_event"), dict) else {}
    provider_item = provider_event.get("item") if isinstance(provider_event.get("item"), dict) else {}
    item_type = str(item.get("type") or provider_item.get("type") or "").strip()
    return item_type == "agent_message"


def _collapse_duplicate_interactive_final_outputs(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    incoming_replies: list[tuple[str, str, datetime | None]] = []
    for entry in entries:
        if str(entry.get("direction") or "").strip().lower() != "in":
            continue
        service_id = _entry_service_id(entry)
        visible_text = _entry_visible_text(entry)
        if not service_id or not visible_text:
            continue
        incoming_replies.append((service_id, visible_text, _interactive_reply_ts(entry)))
    if not incoming_replies:
        return entries

    collapsed: list[dict[str, Any]] = []
    for entry in entries:
        if not _is_duplicate_final_interactive_output(entry):
            collapsed.append(entry)
            continue
        service_id = _entry_service_id(entry)
        visible_text = _entry_visible_text(entry)
        entry_ts = _interactive_reply_ts(entry)
        is_duplicate = False
        for reply_service_id, reply_text, reply_ts in incoming_replies:
            if reply_service_id != service_id or reply_text != visible_text:
                continue
            if entry_ts is None or reply_ts is None or abs((entry_ts - reply_ts).total_seconds()) <= 5:
                is_duplicate = True
                break
        if not is_duplicate:
            collapsed.append(entry)
    return collapsed


def _make_turn_started_entry(record: dict[str, Any], *, service_id: str) -> dict[str, Any]:
    event = {"type": "agent.turn_started"}
    if bool(record.get("goal_manager")):
        event["goal_manager"] = True
    return {
        "direction": "event",
        "ts": str(record.get("ts") or ""),
        "service_id": service_id,
        "event_type": "agent.turn_started",
        "text": f"Agent {service_id} started responding",
        "event": event,
    }


def _make_service_event_entry(record: dict[str, Any], *, service_id: str) -> dict[str, Any]:
    event_type = str(record.get("type") or "event").strip() or "event"
    return {
        "direction": "event",
        "ts": str(record.get("ts") or ""),
        "service_id": service_id,
        "event_type": event_type,
        "text": event_type,
        "event": dict(record),
    }


def _make_goal_audit_provider_entry(record: dict[str, Any], *, service_id: str) -> dict[str, Any] | None:
    provider_event = record.get("provider_event")
    if not isinstance(provider_event, dict):
        return None
    provider_type = str(provider_event.get("type") or "").strip()
    item = provider_event.get("item") if isinstance(provider_event.get("item"), dict) else {}
    if provider_type == "item.completed" and str(item.get("type") or "") == "agent_message":
        text = summarize_provider_event(provider_event)
        if not text:
            return None
        return {
            "direction": "agent",
            "ts": str(record.get("ts") or ""),
            "service_id": service_id,
            "from": service_id,
            "event_type": "service.goal_manager_compact_provider_event.item.completed",
            "text": text,
            "event": {
                "type": "service.goal_manager_compact_provider_event.item.completed",
                "provider_event": provider_event,
                "goal_audit_job_id": record.get("goal_audit_job_id"),
            },
        }
    if provider_type == "agent_message.delta":
        delta = str(provider_event.get("delta") or "").strip()
        if not delta:
            return None
        return {
            "direction": "event",
            "ts": str(record.get("ts") or ""),
            "service_id": service_id,
            "from": service_id,
            "event_type": "agent_message.delta",
            "text": delta,
            "event": {
                "type": "agent_message.delta",
                "delta": delta,
                "provider_event": provider_event,
            },
        }
    if provider_type in {"thread.started", "turn.started", "turn.completed"}:
        return {
            "direction": "event",
            "ts": str(record.get("ts") or ""),
            "service_id": service_id,
            "event_type": "service.goal_manager_compact_provider_event",
            "text": summarize_provider_event(provider_event),
            "event": {
                "type": "service.goal_manager_compact_provider_event",
                "provider_event": provider_event,
                "goal_audit_job_id": record.get("goal_audit_job_id"),
            },
        }
    return None


def _make_provider_event_entry(record: dict[str, Any], *, service_id: str) -> dict[str, Any] | None:
    provider_event = record.get("event")
    if not isinstance(provider_event, dict):
        return None
    provider_type = str(provider_event.get("type") or "").strip()
    item = provider_event.get("item") if isinstance(provider_event.get("item"), dict) else {}
    if provider_type == "item.completed" and str(item.get("type") or "") == "agent_message":
        text = summarize_provider_event(provider_event)
        if not text:
            return None
        return {
            "direction": "agent",
            "ts": str(record.get("ts") or ""),
            "service_id": service_id,
            "from": service_id,
            "event_type": "item.completed",
            "text": text,
            "event": {
                "type": "item.completed",
                "provider_event": provider_event,
            },
        }
    if provider_type == "agent_message.delta":
        delta = str(provider_event.get("delta") or "").strip()
        if not delta:
            return None
        return {
            "direction": "event",
            "ts": str(record.get("ts") or ""),
            "service_id": service_id,
            "from": service_id,
            "event_type": "agent_message.delta",
            "text": delta,
            "event": {
                "type": "agent_message.delta",
                "delta": delta,
                "provider_event": provider_event,
            },
        }
    if provider_type in {"thread.started", "turn.started", "turn.completed"}:
        return {
            "direction": "event",
            "ts": str(record.get("ts") or ""),
            "service_id": service_id,
            "event_type": provider_type,
            "text": summarize_provider_event(provider_event),
            "event": {
                "type": provider_type,
                "provider_event": provider_event,
            },
        }
    return None


def _make_goal_review_entry(review: dict[str, Any]) -> dict[str, Any]:
    event = dict(review)
    event["type"] = "service.goal_audit_completed"
    return {
        "direction": "agent",
        "ts": str(review.get("ts") or ""),
        "service_id": str(review.get("service_id") or ""),
        "event_type": "service.goal_audit_completed",
        "text": goal_audit_history_text(event),
        "event": event,
    }


def build_session_ui_history(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    candidate_service_ids = _candidate_service_ids(runtime_root, username=username, session_id=session_id)
    service_roles = _session_agent_role_map(runtime_root, username=username, session_id=session_id)
    entries: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str, str, str, str, str]] = set()

    def add(entry: dict[str, Any] | None) -> None:
        if not isinstance(entry, dict):
            return
        sanitized = sanitize_history_entry(entry)
        key = _entry_key(sanitized)
        if key in seen_keys:
            return
        seen_keys.add(key)
        entries.append(sanitized)

    for entry in get_user_history(runtime_root, username=username, session_id=session_id):
        if _timeline_entry_is_ui_relevant(entry):
            add(entry)

    http_log_path = _runtime_logs_dir(runtime_root) / "service-http-001.jsonl"
    if http_log_path.exists():
        for record in read_jsonl(http_log_path):
            if str(record.get("type") or "") != "message.in":
                continue
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            if not _scope_matches(record, username=username, session_id=session_id):
                continue
            if str(message.get("type") or "").strip() != "prompt":
                continue
            add(
                {
                    "direction": "in",
                    "ts": str(record.get("ts") or ""),
                    "from": str(message.get("from") or record.get("service_id") or ""),
                    "session_id": session_id,
                    "text": resolve_payload_text(runtime_root, message),
                }
            )

    router_log_path = _runtime_logs_dir(runtime_root) / "router.jsonl"
    if router_log_path.exists():
        for record in read_jsonl(router_log_path):
            if str(record.get("type") or "") != "router.message_forward_attempt":
                continue
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            if str(message.get("to") or "").strip() != "service-http-001":
                continue
            if str(message.get("type") or "").strip() != "event":
                continue
            if not _scope_matches(record, username=username, session_id=session_id):
                continue
            entry = resolve_event_entry(runtime_root, message)
            add(entry)

    for service_id in candidate_service_ids:
        if service_id == "service-http-001":
            continue
        log_path = _runtime_logs_dir(runtime_root) / f"{service_id}.jsonl"
        if not log_path.exists():
            continue
        service_roles_for_id = service_roles.get(service_id, set())
        for record in read_jsonl(log_path):
            if not _scope_matches(record, username=username, session_id=session_id):
                continue
            record_type = str(record.get("type") or "").strip()
            record_slot = _record_session_slot(record)
            worker_only_service = service_roles_for_id == {"worker_agent"}
            interactive_only_service = service_roles_for_id == {"interactive_agent"}
            if (
                record_type in {"agent.turn_started", "service.event"}
                and (record_slot == "worker_agent" or (not record_slot and worker_only_service))
            ):
                continue
            if (
                record_type == "service.event"
                and (record_slot == "interactive_agent" or (not record_slot and interactive_only_service))
                and _record_has_user_visible_provider_text(record)
            ):
                continue
            if (
                record_type == "service.goal_manager_compact_provider_event"
                and _record_has_user_visible_provider_text(record)
            ):
                continue
            if record_type == "agent.turn_started":
                add(_make_turn_started_entry(record, service_id=service_id))
                continue
            if record_type == "service.event":
                add(_make_provider_event_entry(record, service_id=service_id))
                continue
            if record_type == "service.goal_manager_compact_provider_event":
                add(_make_goal_audit_provider_entry(record, service_id=service_id))
                continue
            if record_type in {
                "service.restart_resume_enqueued",
                "service.restart_resume_skipped",
                "service.goal_manager_compact_started",
                "service.goal_manager_compact_failed",
                "service.goal_manager_compact_started",
                "service.goal_manager_compact_checked",
                "service.goal_manager_compact_failed",
                "service.post_turn_followup_started",
                "service.post_turn_followup_failed",
                "service.panic_cleared_after_successful_turn",
                "service.user_response_wait_cleared",
                "service.user_response_wait_timed_out",
            }:
                add(_make_service_event_entry(record, service_id=service_id))

    reviews_path = session_goal_manager_reviews_path(runtime_root, username=username, session_id=session_id)
    if reviews_path.exists():
        for review in read_jsonl(reviews_path):
            add(_make_goal_review_entry(review))

    entries.sort(key=lambda item: str(item.get("ts") or ""))
    entries = _collapse_duplicate_interactive_replies(entries)
    entries = _collapse_duplicate_interactive_final_outputs(entries)
    raw_limit = max(limit * 4, 200)
    if len(entries) > raw_limit:
        entries = entries[-raw_limit:]
    return entries
