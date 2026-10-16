from __future__ import annotations

from typing import Any

from wire.protocol import message_meta_get

DEFAULT_PROVIDER_SESSION_SLOT = "worker_agent"


def normalize_provider_session_slot(value: Any) -> str:
    normalized = "".join(
        ch if ch.isalnum() or ch in {"_", "-", "."} else "_"
        for ch in str(value or "").strip().lower()
    ).strip("._-")
    return normalized or DEFAULT_PROVIDER_SESSION_SLOT


def dispatch_reason(message: dict[str, Any]) -> str:
    payload = message.get("payload")
    if isinstance(payload, dict):
        return str(payload.get("reason") or "").strip().lower()
    return ""


def dispatch_provider_session_slot(
    message: dict[str, Any],
    agent_profile: dict[str, Any] | None = None,
) -> str:
    if isinstance(agent_profile, dict):
        for key in ("session_slot", "lot", "role"):
            raw_value = agent_profile.get(key)
            if isinstance(raw_value, str) and raw_value.strip():
                return normalize_provider_session_slot(raw_value)
    reason = dispatch_reason(message)
    if reason == "http_user_dialogue" or message_meta_get(message, "interactive_agent"):
        return "interactive_agent"
    if reason == "interactive_worker_result":
        return "interactive_agent"
    if reason == "goal_manager_review":
        return "goal_manager"
    return DEFAULT_PROVIDER_SESSION_SLOT


def dispatch_reason_uses_service_pending_only(dispatch_reason_value: str) -> bool:
    return dispatch_reason_value in {
        "http_user_dialogue",
        "interactive_worker_request",
        "interactive_worker_result",
    }


def slot_agent_id(service_id: str, session_id: str, slot: str) -> str:
    return f"{service_id}@@{session_id}@@{slot}"


def interactive_worker_resume_target(
    request_item: dict[str, Any],
    *,
    fallback_service_id: str,
    session_id: str,
) -> tuple[str, str]:
    target_service_id = str(
        request_item.get("interactive_service_id")
        or request_item.get("source_interactive_service_id")
        or fallback_service_id
    ).strip() or fallback_service_id
    target_agent_id = str(
        request_item.get("interactive_agent_id")
        or request_item.get("source_interactive_agent_id")
        or ""
    ).strip()
    if not target_agent_id:
        target_agent_id = slot_agent_id(target_service_id, session_id, "interactive_agent")
    return target_service_id, target_agent_id
