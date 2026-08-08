from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from store_defs import (
    GOAL_MANAGER_ROLE,
    SESSION_RECIPIENT,
    WORKER_AGENT_ROLE,
    normalize_endpoint,
)


@dataclass(frozen=True)
class DispatchLogItem:
    entry: dict[str, Any]
    message: dict[str, Any] | None


@dataclass(frozen=True)
class WakeReason:
    seq: int
    kind: str
    reason: str
    message_id: str = ""
    log_id: str = ""
    available_after: str = ""

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "seq": self.seq,
            "kind": self.kind,
            "reason": self.reason,
            "message_id": self.message_id,
            "log_id": self.log_id,
        }
        if self.available_after:
            value["available_after"] = self.available_after
        return value


@dataclass(frozen=True)
class RoleDispatchReadiness:
    from_log_seq: int
    observed_to_seq: int
    wake_reasons: tuple[WakeReason, ...]
    available_after: str = ""

    def to_readiness_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "from_log_seq": self.from_log_seq,
            "observed_to_seq": self.observed_to_seq,
            "wake_reasons": [reason.to_dict() for reason in self.wake_reasons],
        }
        if self.available_after:
            fields["available_after"] = self.available_after
        return fields


def derive_role_dispatch_readiness(
    items: Iterable[DispatchLogItem],
    *,
    role: str,
    session_id: str,
    active_worker: bool,
) -> RoleDispatchReadiness | None:
    """Return one Role readiness window, or None when no unread entry wakes it."""
    log_items = list(items)
    if not log_items:
        return None

    wake_reasons = tuple(
        reason
        for item in log_items
        if (reason := _wake_reason(item, role=role, session_id=session_id, active_worker=active_worker))
        is not None
    )
    if not wake_reasons:
        return None

    delayed = [reason.available_after for reason in wake_reasons if reason.available_after]
    available_after = "" if len(delayed) != len(wake_reasons) else min(delayed)
    return RoleDispatchReadiness(
        from_log_seq=min(_seq(item.entry) for item in log_items),
        observed_to_seq=max(_seq(item.entry) for item in log_items),
        wake_reasons=tuple(sorted(wake_reasons, key=lambda reason: reason.seq)),
        available_after=available_after,
    )


def _wake_reason(
    item: DispatchLogItem,
    *,
    role: str,
    session_id: str,
    active_worker: bool,
) -> WakeReason | None:
    if role == GOAL_MANAGER_ROLE:
        return _goal_manager_wake_reason(item, session_id=session_id, active_worker=active_worker)
    if role == WORKER_AGENT_ROLE:
        return _worker_wake_reason(item, session_id=session_id)
    return None


def _goal_manager_wake_reason(
    item: DispatchLogItem,
    *,
    session_id: str,
    active_worker: bool,
) -> WakeReason | None:
    entry = item.entry
    if entry.get("kind") == "SessionActiveChanged":
        event = entry.get("event")
        if isinstance(event, dict) and event.get("active") is True:
            return _reason(item, "SessionActivated", "Session became active with an incomplete Goal.")
        return None
    if entry.get("kind") == "GoalStateChanged":
        event = entry.get("event")
        if not isinstance(event, dict) or event.get("completion_state") != "incomplete":
            return None
        if event.get("defer_goal_manager_until_worker_report") is True:
            return None
        if str(event.get("actor") or "") == normalize_endpoint(GOAL_MANAGER_ROLE, session_id=session_id):
            return None
        if active_worker:
            return None
        return _reason(item, "GoalIncomplete", "SessionGoal became incomplete outside GoalManager.")
    if entry.get("kind") == "SystemSignal":
        return _system_signal_reason(item, role=GOAL_MANAGER_ROLE)

    message = item.message
    payload = message.get("payload") if message else None
    if not isinstance(payload, dict):
        return None
    if payload.get("user_input") is True:
        if payload.get("worker_request") is True or payload.get("defer_goal_manager_until_worker_report") is True:
            return None
        return _reason(item, "UserInput", "New UserInput requires GoalManager review.")
    if payload.get("schedule_update") is True:
        return _reason(item, "ScheduleUpdate", "A Unit schedule update requires GoalManager review.")
    if (
        str(message.get("from") or "") == normalize_endpoint(WORKER_AGENT_ROLE, session_id=session_id)
        and str(message.get("to") or "") == normalize_endpoint(SESSION_RECIPIENT, session_id=session_id)
    ):
        return _reason(item, "WorkerReport", "A WorkerAgent Session report requires GoalManager review.")
    return None


def _worker_wake_reason(item: DispatchLogItem, *, session_id: str) -> WakeReason | None:
    entry = item.entry
    if entry.get("kind") == "SystemSignal":
        return _system_signal_reason(item, role=WORKER_AGENT_ROLE)

    message = item.message
    payload = message.get("payload") if message else None
    if not isinstance(payload, dict) or payload.get("worker_request") is not True:
        return None
    if str(message.get("to") or "") != normalize_endpoint(SESSION_RECIPIENT, session_id=session_id):
        return None
    if str(message.get("from") or "") == normalize_endpoint(GOAL_MANAGER_ROLE, session_id=session_id):
        return _reason(item, "WorkerRequest", "GoalManager requested WorkerAgent work.")
    if payload.get("worker_followup") is True:
        return _reason(item, "WorkerFollowup", "New UserInput must reach the active WorkerAgent.")
    return None


def _system_signal_reason(item: DispatchLogItem, *, role: str) -> WakeReason | None:
    event = item.entry.get("event")
    if not isinstance(event, dict):
        return None
    target_roles = event.get("target_roles")
    if isinstance(target_roles, list) and role not in target_roles:
        return None
    data = event.get("data")
    available_after = str(data.get("available_after") or "").strip() if isinstance(data, dict) else ""
    signal_type = str(event.get("signal_type") or "system")
    return _reason(
        item,
        "SystemSignal",
        f"System signal {signal_type} requires {role} processing.",
        available_after=available_after,
    )


def _reason(
    item: DispatchLogItem,
    kind: str,
    reason: str,
    *,
    available_after: str = "",
) -> WakeReason:
    message_id = str(item.message.get("message_id") or "") if item.message else ""
    return WakeReason(
        seq=_seq(item.entry),
        kind=kind,
        reason=reason,
        message_id=message_id,
        log_id=str(item.entry.get("log_id") or ""),
        available_after=available_after,
    )


def _seq(entry: dict[str, Any]) -> int:
    return int(entry.get("seq") or 0)
