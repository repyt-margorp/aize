from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from runtime.event_log import summarize_provider_event
from runtime.goal_persist import goal_audit_history_text
from runtime.message_builder import resolve_event_entry, resolve_payload_text
from runtime.persistent_state import (
    get_history as get_user_history,
    get_session_service,
    list_session_agent_contacts,
    read_jsonl,
    session_goal_manager_reviews_path,
    session_services_dir,
)
from runtime.persistent_state_pkg.history import sanitize_history_entry


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


def _timeline_entry_is_ui_relevant(entry: dict[str, Any]) -> bool:
    direction = str(entry.get("direction") or "").strip().lower()
    if direction in {"out", "user"}:
        return True
    if direction == "in":
        source = str(entry.get("from") or "").strip()
        return not source.startswith("service-")
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
            "event_type": "service.goal_audit_provider_event.item.completed",
            "text": text,
            "event": {
                "type": "service.goal_audit_provider_event.item.completed",
                "provider_event": provider_event,
                "goal_audit_job_id": record.get("goal_audit_job_id"),
            },
        }
    if provider_type in {"thread.started", "turn.started", "turn.completed"}:
        return {
            "direction": "event",
            "ts": str(record.get("ts") or ""),
            "service_id": service_id,
            "event_type": "service.goal_audit_provider_event",
            "text": summarize_provider_event(provider_event),
            "event": {
                "type": "service.goal_audit_provider_event",
                "provider_event": provider_event,
                "goal_audit_job_id": record.get("goal_audit_job_id"),
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
        for record in read_jsonl(log_path):
            if not _scope_matches(record, username=username, session_id=session_id):
                continue
            record_type = str(record.get("type") or "").strip()
            if record_type == "agent.turn_started":
                add(_make_turn_started_entry(record, service_id=service_id))
                continue
            if record_type == "service.goal_audit_provider_event":
                add(_make_goal_audit_provider_entry(record, service_id=service_id))
                continue
            if record_type in {
                "service.restart_resume_enqueued",
                "service.restart_resume_skipped",
                "service.goal_audit_started",
                "service.goal_audit_failed",
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
    raw_limit = max(limit * 4, 200)
    if len(entries) > raw_limit:
        entries = entries[-raw_limit:]
    return entries
