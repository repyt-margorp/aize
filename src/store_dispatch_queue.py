from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from model import new_id, utc_now
from store_defs import (
    DISPATCH_PRIORITY_GOAL,
    GOAL_MANAGER_ROLE,
    WORKER_AGENT_ROLE,
)


class DispatchQueueMixin:
    def _enqueue_dispatch(
        self,
        state: dict[str, Any],
        goal: dict[str, Any],
        *,
        priority: int,
        reason: str,
        role: str = GOAL_MANAGER_ROLE,
        queued_at: str | None = None,
        available_after: str | None = None,
        trigger_message_id: str | None = None,
    ) -> dict[str, Any] | None:
        goal_id = str(goal.get("goal_id") or "")
        session_id = str(goal.get("session_id") or "")
        if not goal_id or not session_id:
            return None
        session = state["sessions"].get(session_id)
        if not session or session.get("active") is not True:
            return None
        if goal.get("archived_at"):
            return None
        if goal.get("completion_state") != "incomplete":
            return None
        queue = state.setdefault("dispatch_queue", [])
        for entry in queue:
            entry_trigger_message_id = str(entry.get("trigger_message_id") or "")
            if trigger_message_id:
                if entry_trigger_message_id != trigger_message_id:
                    continue
            elif entry_trigger_message_id:
                continue
            if (
                entry.get("goal_id") == goal_id
                and entry.get("status") == "queued"
                and entry.get("role", GOAL_MANAGER_ROLE) == role
            ):
                existing_priority = int(entry.get("priority") or 0)
                changed = False
                if priority >= existing_priority:
                    if entry.get("priority") != priority:
                        entry["priority"] = priority
                        changed = True
                    if entry.get("reason") != reason:
                        entry["reason"] = reason
                        changed = True
                    if available_after:
                        if entry.get("available_after") != available_after:
                            entry["available_after"] = available_after
                            changed = True
                    elif "available_after" in entry:
                        entry.pop("available_after", None)
                        changed = True
                    if trigger_message_id and entry.get("trigger_message_id") != trigger_message_id:
                        entry["trigger_message_id"] = trigger_message_id
                        changed = True
                if changed:
                    entry["updated_at"] = queued_at or utc_now()
                return entry
        entry = {
            "queue_id": new_id("dq"),
            "session_id": session_id,
            "goal_id": goal_id,
            "role": role,
            "priority": priority,
            "reason": reason,
            "status": "queued",
            "queued_at": queued_at or utc_now(),
        }
        if available_after:
            entry["available_after"] = available_after
        if trigger_message_id:
            entry["trigger_message_id"] = trigger_message_id
        queue.append(entry)
        return entry

    def _resolve_dispatch_queue_entries(self, state: dict[str, Any], goal_id: str, *, resolved_at: str) -> None:
        for entry in state.setdefault("dispatch_queue", []):
            if entry.get("goal_id") != goal_id:
                continue
            if entry.get("status") not in {"queued", "acquired"}:
                continue
            entry["status"] = "resolved"
            entry["resolved_at"] = resolved_at

    def _enqueue_dispatchable_goals(self, state: dict[str, Any]) -> None:
        for goal in state.get("goals", {}).values():
            if goal.get("archived_at"):
                continue
            if goal.get("completion_state") != "incomplete":
                continue
            goal_id = str(goal.get("goal_id") or "")
            if self._has_live_worker_signal_for_goal(state, goal_id):
                continue
            if any(
                entry.get("goal_id") == goal_id
                and entry.get("status") in {"queued", "acquired"}
                and entry.get("role", GOAL_MANAGER_ROLE) == GOAL_MANAGER_ROLE
                for entry in state.setdefault("dispatch_queue", [])
            ):
                continue
            self._enqueue_dispatch(
                state,
                goal,
                priority=DISPATCH_PRIORITY_GOAL,
                reason="Active incomplete Goal discovered.",
                role=GOAL_MANAGER_ROLE,
            )

    def _next_dispatch_queue_entry(
        self,
        state: dict[str, Any],
        *,
        session_id: str | None = None,
    ) -> dict[str, Any] | None:
        self._enqueue_dispatchable_goals(state)
        candidates: list[tuple[int, dict[str, Any]]] = []
        for queue_index, entry in enumerate(state.setdefault("dispatch_queue", [])):
            if entry.get("status") != "queued":
                continue
            if session_id and str(entry.get("session_id") or "") != session_id:
                continue
            if not self._queue_entry_is_available(entry):
                continue
            goal = state["goals"].get(str(entry.get("goal_id") or ""))
            session = state["sessions"].get(str(entry.get("session_id") or ""))
            if not goal or goal.get("archived_at") or goal.get("completion_state") != "incomplete":
                entry["status"] = "stale"
                entry["stale_at"] = utc_now()
                continue
            if not session or session.get("active") is not True:
                continue
            entry_role = str(entry.get("role") or GOAL_MANAGER_ROLE)
            if self._has_acquired_role_run(
                state,
                session_id=str(entry.get("session_id") or ""),
                role=entry_role,
            ):
                continue
            candidates.append((queue_index, entry))
        if not candidates:
            return None
        return sorted(
            candidates,
            key=lambda item: (
                -int(item[1].get("priority") or 0),
                str(item[1].get("queued_at") or ""),
                item[0],
            ),
        )[0][1]

    def _queue_entry_is_available(self, entry: dict[str, Any]) -> bool:
        available_after = str(entry.get("available_after") or "").strip()
        if not available_after:
            return True
        try:
            available_at = datetime.fromisoformat(available_after.replace("Z", "+00:00"))
        except ValueError:
            return True
        return available_at <= datetime.now(UTC)

    def _has_acquired_role_run(self, state: dict[str, Any], *, session_id: str, role: str) -> bool:
        if role not in {GOAL_MANAGER_ROLE, WORKER_AGENT_ROLE}:
            return False
        return any(
            str(run.get("session_id") or "") == session_id
            and str(run.get("role") or "") == role
            and run.get("lease_state") == "acquired"
            for run in state.get("dispatch_runs", {}).values()
        )

    def _has_live_worker_signal_for_goal(self, state: dict[str, Any], goal_id: str) -> bool:
        if not goal_id:
            return False
        if any(
            entry.get("goal_id") == goal_id
            and entry.get("role", GOAL_MANAGER_ROLE) == WORKER_AGENT_ROLE
            and entry.get("status") in {"queued", "acquired"}
            for entry in state.setdefault("dispatch_queue", [])
        ):
            return True
        return any(
            run.get("goal_id") == goal_id
            and run.get("role") == WORKER_AGENT_ROLE
            and run.get("lease_state") == "acquired"
            for run in state.get("dispatch_runs", {}).values()
        )

    def _queue_entry_by_id(self, state: dict[str, Any], queue_id: str) -> dict[str, Any] | None:
        for entry in state.setdefault("dispatch_queue", []):
            if entry.get("queue_id") == queue_id:
                return entry
        return None
