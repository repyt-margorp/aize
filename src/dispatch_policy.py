from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Collection

from store_defs import GOAL_MANAGER_ROLE, WORKER_AGENT_ROLE


DEFAULT_SCHEDULING_CLASS = "normal"
DEFAULT_BASE_PRIORITY = 0
ROLE_BASE_PRIORITIES = {
    GOAL_MANAGER_ROLE: 0,
    WORKER_AGENT_ROLE: 0,
}
SCHEDULING_CLASS_SCORES = {
    "idle": -100,
    "background": -20,
    "normal": 0,
    "high": 20,
    "critical": 100,
}
AGING_INTERVAL_SECONDS = 60
AGING_SCORE_LIMIT = 100


@dataclass(frozen=True)
class SchedulingDecision:
    readiness_index: int | None
    stale_indexes: tuple[int, ...] = ()
    scheduling_score: int | None = None
    scheduling_reason: str = ""


def select_role_dispatch_readiness(
    readiness_entries: list[dict[str, Any]],
    *,
    goals: dict[str, dict[str, Any]],
    sessions: dict[str, dict[str, Any]],
    acquired_roles: Collection[tuple[str, str]],
    session_id: str | None = None,
    now: datetime | None = None,
) -> SchedulingDecision:
    current_time = now or datetime.now(UTC)
    candidates: list[tuple[int, dict[str, Any], int, str]] = []
    stale_indexes: list[int] = []

    for index, entry in enumerate(readiness_entries):
        if entry.get("status") != "ready":
            continue
        entry_session_id = str(entry.get("session_id") or "")
        if session_id and entry_session_id != session_id:
            continue
        if not readiness_is_available(entry, now=current_time):
            continue

        goal = goals.get(str(entry.get("goal_id") or ""))
        if not goal or goal.get("archived_at") or goal.get("completion_state") != "incomplete":
            stale_indexes.append(index)
            continue
        session = sessions.get(entry_session_id)
        if not session or session.get("active") is not True:
            continue
        role = str(entry.get("role") or GOAL_MANAGER_ROLE)
        if (entry_session_id, role) in acquired_roles:
            continue
        score, reason = scheduling_score(session, role=role, first_ready_at=entry.get("first_ready_at"), now=current_time)
        candidates.append((index, entry, score, reason))

    if not candidates:
        return SchedulingDecision(None, tuple(stale_indexes))
    selected = min(
        candidates,
        key=lambda item: (
            -item[2],
            str(item[1].get("first_ready_at") or ""),
            str(item[1].get("session_id") or ""),
            str(item[1].get("role") or ""),
            item[0],
        ),
    )
    return SchedulingDecision(selected[0], tuple(stale_indexes), selected[2], selected[3])


def scheduling_score(
    session: dict[str, Any],
    *,
    role: str,
    first_ready_at: Any,
    now: datetime,
) -> tuple[int, str]:
    policy = session.get("scheduling_policy")
    if not isinstance(policy, dict):
        policy = {}
    scheduling_class = str(policy.get("class") or DEFAULT_SCHEDULING_CLASS)
    class_score = SCHEDULING_CLASS_SCORES.get(scheduling_class, SCHEDULING_CLASS_SCORES[DEFAULT_SCHEDULING_CLASS])
    base_priority = int(policy.get("base_priority") or DEFAULT_BASE_PRIORITY)
    role_priority = int(ROLE_BASE_PRIORITIES.get(role, 0))
    age_seconds = _waiting_seconds(first_ready_at, now=now)
    age_score = min(AGING_SCORE_LIMIT, int(age_seconds // AGING_INTERVAL_SECONDS))
    score = class_score + base_priority + role_priority + age_score
    reason = (
        f"class={scheduling_class}({class_score}); session={base_priority}; "
        f"role={role_priority}; waiting_age={age_score}"
    )
    return score, reason


def readiness_is_available(entry: dict[str, Any], *, now: datetime | None = None) -> bool:
    available_after = str(entry.get("available_after") or "").strip()
    if not available_after:
        return True
    available_at = _parse_timestamp(available_after)
    return True if available_at is None else available_at <= (now or datetime.now(UTC))


def _waiting_seconds(value: Any, *, now: datetime) -> float:
    ready_at = _parse_timestamp(str(value or ""))
    if ready_at is None:
        return 0.0
    return max(0.0, (now - ready_at).total_seconds())


def _parse_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
