from __future__ import annotations

from typing import Any

from dispatch_policy import select_role_dispatch_readiness
from dispatch_projection import DispatchLogItem, derive_role_dispatch_readiness
from model import new_id, utc_now
from store_defs import GOAL_MANAGER_ROLE, WORKER_AGENT_ROLE


class DispatchReadinessMixin:
    def _ensure_dispatch_readiness_state(self, state: dict[str, Any]) -> bool:
        if "dispatch_readiness" in state:
            return False
        state["dispatch_readiness"] = []
        return True

    def _resolve_dispatch_readiness(self, state: dict[str, Any], goal_id: str, *, resolved_at: str) -> None:
        for entry in state.setdefault("dispatch_readiness", []):
            if entry.get("goal_id") != goal_id or entry.get("status") not in {"ready", "acquired"}:
                continue
            entry["status"] = "resolved"
            entry["resolved_at"] = resolved_at

    def _refresh_dispatch_readiness(self, state: dict[str, Any]) -> bool:
        previous = [dict(entry) for entry in state.setdefault("dispatch_readiness", [])]
        state["dispatch_readiness"] = [
            entry
            for entry in state["dispatch_readiness"]
            if entry.get("status") in {"ready", "acquired"}
        ]
        self._deduplicate_ready_entries(state)

        for goal in self._current_goals(state):
            if goal.get("archived_at") or goal.get("completion_state") != "incomplete":
                continue
            session_id = str(goal.get("session_id") or "")
            session = state.get("sessions", {}).get(session_id)
            if not session or session.get("active") is not True:
                continue
            for role in (GOAL_MANAGER_ROLE, WORKER_AGENT_ROLE):
                if self._has_acquired_role_run(state, session_id=session_id, role=role):
                    continue
                if self._acquired_readiness_for_role(state, session_id=session_id, role=role):
                    continue
                fields = self._readiness_fields_from_session_log(
                    state,
                    session_id=session_id,
                    role=role,
                )
                self._reconcile_ready_entry(state, goal=goal, role=role, fields=fields)
        return previous != state["dispatch_readiness"]

    def _deduplicate_ready_entries(self, state: dict[str, Any]) -> None:
        retained: list[dict[str, Any]] = []
        by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for entry in state.setdefault("dispatch_readiness", []):
            if entry.get("status") != "ready":
                retained.append(entry)
                continue
            key = (str(entry.get("session_id") or ""), str(entry.get("role") or GOAL_MANAGER_ROLE))
            existing = by_key.get(key)
            if existing is None:
                by_key[key] = entry
                retained.append(entry)
                continue
            if str(entry.get("first_ready_at") or "") < str(existing.get("first_ready_at") or ""):
                retained.remove(existing)
                retained.append(entry)
                by_key[key] = entry
        state["dispatch_readiness"] = retained

    def _reconcile_ready_entry(
        self,
        state: dict[str, Any],
        *,
        goal: dict[str, Any],
        role: str,
        fields: dict[str, Any] | None,
    ) -> None:
        session_id = str(goal.get("session_id") or "")
        current = next(
            (
                entry
                for entry in state.setdefault("dispatch_readiness", [])
                if entry.get("status") == "ready"
                and str(entry.get("session_id") or "") == session_id
                and str(entry.get("role") or GOAL_MANAGER_ROLE) == role
            ),
            None,
        )
        if fields is None:
            if current is not None:
                state["dispatch_readiness"].remove(current)
            return

        now = utc_now()
        if current is None:
            entry = {
                "readiness_id": new_id("ready"),
                "session_id": session_id,
                "goal_id": str(goal.get("goal_id") or ""),
                "role": role,
                "status": "ready",
                "first_ready_at": now,
                "refreshed_at": now,
                **fields,
            }
            state["dispatch_readiness"].append(entry)
            return

        current["goal_id"] = str(goal.get("goal_id") or "")
        for key in ("from_log_seq", "observed_to_seq", "wake_reasons", "available_after"):
            if key in fields:
                value = fields[key]
                current[key] = [dict(item) for item in value] if key == "wake_reasons" else value
            else:
                current.pop(key, None)
        current["refreshed_at"] = now

    def _readiness_fields_from_session_log(
        self,
        state: dict[str, Any],
        *,
        session_id: str,
        role: str,
    ) -> dict[str, Any] | None:
        entries = self._session_log_entries_after_cursor(state, session_id=session_id, role=role)
        readiness = derive_role_dispatch_readiness(
            (
                DispatchLogItem(entry=entry, message=self._message_for_log_entry(state, entry))
                for entry in entries
            ),
            role=role,
            session_id=session_id,
            active_worker=bool(self._active_worker_run_for_session(state, session_id=session_id)),
        )
        return readiness.to_readiness_fields() if readiness else None

    def _next_dispatch_readiness_entry(
        self,
        state: dict[str, Any],
        *,
        session_id: str | None = None,
    ) -> dict[str, Any] | None:
        self._refresh_dispatch_readiness(state)
        entries = state.setdefault("dispatch_readiness", [])
        acquired_roles = {
            (str(run.get("session_id") or ""), active_role)
            for run in state.get("dispatch_runs", {}).values()
            if run.get("lease_state") == "acquired"
            for active_role in {str(run.get("role") or ""), str(run.get("current_phase") or "")}
            if active_role
        }
        decision = select_role_dispatch_readiness(
            entries,
            goals=state["goals"],
            sessions=state["sessions"],
            acquired_roles=acquired_roles,
            session_id=session_id,
        )
        if decision.stale_indexes:
            stale_at = utc_now()
            for index in decision.stale_indexes:
                entries[index]["status"] = "stale"
                entries[index]["stale_at"] = stale_at
        if decision.readiness_index is None:
            return None
        selected = entries[decision.readiness_index]
        selected["_scheduling_score"] = decision.scheduling_score
        selected["_scheduling_reason"] = decision.scheduling_reason
        return selected

    def _has_acquired_role_run(self, state: dict[str, Any], *, session_id: str, role: str) -> bool:
        if role not in {GOAL_MANAGER_ROLE, WORKER_AGENT_ROLE}:
            return False
        return any(
            str(run.get("session_id") or "") == session_id
            and (str(run.get("role") or "") == role or str(run.get("current_phase") or "") == role)
            and run.get("lease_state") == "acquired"
            for run in state.get("dispatch_runs", {}).values()
        )

    def _acquired_readiness_for_role(
        self,
        state: dict[str, Any],
        *,
        session_id: str,
        role: str,
    ) -> dict[str, Any] | None:
        return next(
            (
                entry
                for entry in state.setdefault("dispatch_readiness", [])
                if entry.get("status") == "acquired"
                and str(entry.get("session_id") or "") == session_id
                and str(entry.get("role") or GOAL_MANAGER_ROLE) == role
            ),
            None,
        )

    def _has_live_worker_readiness_for_goal(self, state: dict[str, Any], goal_id: str) -> bool:
        if not goal_id:
            return False
        if any(
            entry.get("goal_id") == goal_id
            and entry.get("role", GOAL_MANAGER_ROLE) == WORKER_AGENT_ROLE
            and entry.get("status") in {"ready", "acquired"}
            for entry in state.setdefault("dispatch_readiness", [])
        ):
            return True
        return any(
            run.get("goal_id") == goal_id
            and run.get("role") == WORKER_AGENT_ROLE
            and run.get("lease_state") == "acquired"
            for run in state.get("dispatch_runs", {}).values()
        )

    def _readiness_entry_by_id(self, state: dict[str, Any], readiness_id: str) -> dict[str, Any] | None:
        return next(
            (
                entry
                for entry in state.setdefault("dispatch_readiness", [])
                if entry.get("readiness_id") == readiness_id
            ),
            None,
        )
