from __future__ import annotations

from typing import Any


DEFAULT_DISPATCH_PRIORITY = 50

DISPATCH_REASON_PRIORITIES: dict[str, int] = {
    "panic_recovery": 100,
    "child_session_panic": 95,
    "child_session_completed": 90,
    "goal_manager_review": 80,
    "session_created": 75,
    "goal_saved": 75,
    "goal_state_changed": 74,
    "restart_resume": 72,
    "scheduled_resume": 70,
    "http_user_dialogue": 65,
    "interactive_worker_request": 64,
    "interactive_worker_result": 63,
    "goal_feedback": 60,
    "turn_completed": 55,
}


def normalize_dispatch_reason(value: Any) -> str:
    return str(value or "").strip().lower()


def dispatch_priority(reason: Any) -> int:
    return DISPATCH_REASON_PRIORITIES.get(normalize_dispatch_reason(reason), DEFAULT_DISPATCH_PRIORITY)


def dispatch_message_priority(message: dict[str, Any]) -> int:
    meta = message.get("meta")
    if isinstance(meta, dict):
        raw_priority = meta.get("dispatch_priority")
        if isinstance(raw_priority, int):
            return raw_priority
        try:
            if raw_priority is not None:
                return int(raw_priority)
        except (TypeError, ValueError):
            pass
    payload = message.get("payload")
    reason = payload.get("reason") if isinstance(payload, dict) else ""
    return dispatch_priority(reason)


def order_dispatch_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        message
        for _, _, message in sorted(
            (
                (-dispatch_message_priority(message), index, message)
                for index, message in enumerate(messages)
            )
        )
    ]
