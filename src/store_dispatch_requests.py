from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from model import new_id, utc_now
from store_defs import (
    DISPATCH_PRIORITY_GOAL,
    DISPATCH_PRIORITY_USER_INPUT,
    DISPATCH_PRIORITY_WORKER_REQUEST,
    GOAL_MANAGER_ROLE,
    SESSION_RECIPIENT,
    WORKER_AGENT_ROLE,
    normalize_endpoint,
)


class DispatchRequestMixin:
    def _ensure_dispatch_requests_state(self, state: dict[str, Any]) -> bool:
        if "dispatch_requests" in state:
            return False
        state["dispatch_requests"] = []
        return True

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
        from_log_seq: int | None = None,
        to_log_seq: int | None = None,
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
        requests = state.setdefault("dispatch_requests", [])
        for entry in requests:
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
                    if from_log_seq is not None and entry.get("from_log_seq") != from_log_seq:
                        entry["from_log_seq"] = from_log_seq
                        changed = True
                    if to_log_seq is not None and entry.get("to_log_seq") != to_log_seq:
                        entry["to_log_seq"] = to_log_seq
                        changed = True
                if changed:
                    entry["updated_at"] = queued_at or utc_now()
                return entry
        entry = {
            "request_id": new_id("dr"),
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
        if from_log_seq is not None:
            entry["from_log_seq"] = from_log_seq
        if to_log_seq is not None:
            entry["to_log_seq"] = to_log_seq
        requests.append(entry)
        return entry

    def _resolve_dispatch_request_entries(self, state: dict[str, Any], goal_id: str, *, resolved_at: str) -> None:
        for entry in state.setdefault("dispatch_requests", []):
            if entry.get("goal_id") != goal_id:
                continue
            if entry.get("status") not in {"queued", "acquired"}:
                continue
            entry["status"] = "resolved"
            entry["resolved_at"] = resolved_at

    def _enqueue_dispatchable_goals(self, state: dict[str, Any]) -> None:
        state["dispatch_requests"] = [
            entry
            for entry in state.setdefault("dispatch_requests", [])
            if entry.get("status") in {"queued", "acquired"}
        ]
        for goal in self._current_goals(state):
            if goal.get("archived_at"):
                continue
            if goal.get("completion_state") != "incomplete":
                continue
            session_id = str(goal.get("session_id") or "")
            session = state.get("sessions", {}).get(session_id)
            if not session or session.get("active") is not True:
                continue
            goal_id = str(goal.get("goal_id") or "")
            for role in (WORKER_AGENT_ROLE, GOAL_MANAGER_ROLE):
                if self._has_acquired_role_run(state, session_id=session_id, role=role):
                    continue
                if any(
                    entry.get("goal_id") == goal_id
                    and entry.get("status") in {"queued", "acquired"}
                    and entry.get("role", GOAL_MANAGER_ROLE) == role
                    for entry in state.setdefault("dispatch_requests", [])
                ):
                    continue
                request = self._dispatch_request_from_session_log(
                    state,
                    goal=goal,
                    session_id=session_id,
                    role=role,
                )
                if request:
                    self._enqueue_dispatch(state, goal, role=role, **request)

    def _dispatch_request_from_session_log(
        self,
        state: dict[str, Any],
        *,
        goal: dict[str, Any],
        session_id: str,
        role: str,
    ) -> dict[str, Any] | None:
        entries = self._session_log_entries_after_cursor(state, session_id=session_id, role=role)
        if not entries:
            return None
        relevant: list[tuple[dict[str, Any], dict[str, Any] | None, int, str]] = []
        for entry in entries:
            message = self._message_for_log_entry(state, entry)
            if role == GOAL_MANAGER_ROLE:
                priority_reason = self._goal_manager_relevance(state, entry, message, session_id=session_id)
            else:
                priority_reason = self._worker_relevance(entry, message, session_id=session_id)
            if priority_reason is None:
                continue
            priority, reason = priority_reason
            relevant.append((entry, message, priority, reason))
        if not relevant:
            return None
        trigger_entry, trigger_message, priority, reason = sorted(
            relevant,
            key=lambda item: (int(item[2]), int(item[0].get("seq") or 0)),
        )[-1]
        first_seq = min(int(entry.get("seq") or 0) for entry in entries)
        to_seq = max(int(entry.get("seq") or 0) for entry in entries)
        request = {
            "priority": priority,
            "reason": reason,
            "from_log_seq": first_seq,
            "to_log_seq": to_seq,
        }
        if trigger_entry.get("kind") == "SystemSignal":
            event = trigger_entry.get("event")
            data = event.get("data") if isinstance(event, dict) else None
            available_after = str(data.get("available_after") or "").strip() if isinstance(data, dict) else ""
            if available_after:
                request["available_after"] = available_after
        if trigger_message:
            request["trigger_message_id"] = str(trigger_message.get("message_id") or "")
        return request

    def _goal_manager_relevance(
        self,
        state: dict[str, Any],
        entry: dict[str, Any],
        message: dict[str, Any] | None,
        *,
        session_id: str,
    ) -> tuple[int, str] | None:
        if entry.get("kind") == "SessionActiveChanged":
            event = entry.get("event")
            if isinstance(event, dict) and event.get("active") is True:
                return DISPATCH_PRIORITY_GOAL, "Session became active with incomplete Goal."
            return None
        if entry.get("kind") == "GoalStateChanged":
            event = entry.get("event")
            if not isinstance(event, dict):
                return None
            if event.get("completion_state") != "incomplete":
                return None
            actor = str(event.get("actor") or "")
            if actor == normalize_endpoint(GOAL_MANAGER_ROLE, session_id=session_id):
                return None
            if self._active_worker_run_for_session(state, session_id=session_id):
                return None
            return DISPATCH_PRIORITY_GOAL, "SessionGoal became incomplete."
        if entry.get("kind") == "SystemSignal":
            return self._system_signal_relevance(entry, role=GOAL_MANAGER_ROLE)
        if not message:
            return None
        payload = message.get("payload")
        if not isinstance(payload, dict):
            return None
        if payload.get("user_input") is True:
            if payload.get("worker_request") is True or payload.get("defer_goal_manager_until_worker_report") is True:
                return None
            return DISPATCH_PRIORITY_USER_INPUT, f"UserInput message {message.get('message_id')} requires GoalManager review."
        if payload.get("schedule_update") is True:
            return DISPATCH_PRIORITY_GOAL, f"Schedule update {message.get('message_id')} requires GoalManager review."
        if (
            str(message.get("from") or "") == normalize_endpoint(WORKER_AGENT_ROLE, session_id=session_id)
            and str(message.get("to") or "") == normalize_endpoint(SESSION_RECIPIENT, session_id=session_id)
        ):
            return DISPATCH_PRIORITY_GOAL, f"WorkerAgent Session report {message.get('message_id')} requires GoalManager review."
        return None

    def _worker_relevance(
        self,
        entry: dict[str, Any],
        message: dict[str, Any] | None,
        *,
        session_id: str,
    ) -> tuple[int, str] | None:
        if entry.get("kind") == "SystemSignal":
            return self._system_signal_relevance(entry, role=WORKER_AGENT_ROLE)
        if not message:
            return None
        payload = message.get("payload")
        if not isinstance(payload, dict):
            return None
        if payload.get("worker_request") is not True:
            return None
        if str(message.get("to") or "") != normalize_endpoint(SESSION_RECIPIENT, session_id=session_id):
            return None
        if str(message.get("from") or "") == normalize_endpoint(GOAL_MANAGER_ROLE, session_id=session_id):
            return DISPATCH_PRIORITY_WORKER_REQUEST, f"GoalManager worker request {message.get('message_id')} requires WorkerAgent work."
        if payload.get("worker_followup") is True:
            return DISPATCH_PRIORITY_WORKER_REQUEST, f"UserInput follow-up {message.get('message_id')} should reach WorkerAgent."
        return None

    def _system_signal_relevance(self, entry: dict[str, Any], *, role: str) -> tuple[int, str] | None:
        event = entry.get("event")
        if not isinstance(event, dict):
            return None
        target_roles = event.get("target_roles")
        if isinstance(target_roles, list) and role not in target_roles:
            return None
        signal_type = str(event.get("signal_type") or "system")
        signal_id = str(event.get("signal_id") or entry.get("log_id") or "")
        priority = DISPATCH_PRIORITY_WORKER_REQUEST if role == WORKER_AGENT_ROLE else DISPATCH_PRIORITY_GOAL
        return priority, f"System signal {signal_type} {signal_id} requires {role} processing."

    def _next_dispatch_requests_entry(
        self,
        state: dict[str, Any],
        *,
        session_id: str | None = None,
    ) -> dict[str, Any] | None:
        self._enqueue_dispatchable_goals(state)
        candidates: list[tuple[int, dict[str, Any]]] = []
        for request_index, entry in enumerate(state.setdefault("dispatch_requests", [])):
            if entry.get("status") != "queued":
                continue
            if session_id and str(entry.get("session_id") or "") != session_id:
                continue
            if not self._request_entry_is_available(entry):
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
            candidates.append((request_index, entry))
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

    def _request_entry_is_available(self, entry: dict[str, Any]) -> bool:
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
            and (str(run.get("role") or "") == role or str(run.get("current_phase") or "") == role)
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
            for entry in state.setdefault("dispatch_requests", [])
        ):
            return True
        return any(
            run.get("goal_id") == goal_id
            and run.get("role") == WORKER_AGENT_ROLE
            and run.get("lease_state") == "acquired"
            for run in state.get("dispatch_runs", {}).values()
        )

    def _request_entry_by_id(self, state: dict[str, Any], request_id: str) -> dict[str, Any] | None:
        for entry in state.setdefault("dispatch_requests", []):
            if entry.get("request_id") == request_id:
                return entry
        return None
