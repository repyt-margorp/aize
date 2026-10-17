from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from goal.policy import select_role_dispatch_readiness
from goal.projections import DispatchLogItem, derive_role_dispatch_readiness
from kernel.model import new_id, utc_now
from goal.defs import DISPATCH_LEASE_RECOVERY_SECONDS, GOAL_MANAGER_ROLE, WORKER_AGENT_ROLE


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
        waiting_changed = self._expire_goal_waiting(state)
        previous = [dict(entry) for entry in state.setdefault("dispatch_readiness", [])]
        state["dispatch_readiness"] = [
            entry
            for entry in state["dispatch_readiness"]
            if entry.get("status") in {"ready", "acquired"}
        ]
        self._deduplicate_ready_entries(state)

        for goal in self._current_goals(state):
            if goal.get("archived_at") or goal.get("completion_state") != "incomplete" or goal.get("waiting"):
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
        return previous != state["dispatch_readiness"] or waiting_changed

    def _expire_goal_waiting(self, state: dict[str, Any]) -> bool:
        superseded = False
        now = utc_now()
        for goal in self._current_goals(state):
            waiting = goal.get("waiting")
            if not isinstance(waiting, dict) or not waiting.get("kind"):
                continue
            session_id = str(goal.get("session_id") or "")
            session = state.get("sessions", {}).get(session_id)
            if not session or session.get("active") is not True:
                continue
            reason = self._goal_waiting_wake_reason(state, goal, waiting, now=now)
            if not reason:
                continue
            goal.pop("waiting", None)
            self._log_system_signal(
                state,
                session_id,
                signal_type="GoalWaitingWake",
                body=reason,
                target_roles=[GOAL_MANAGER_ROLE],
                actor="scheduler",
                data={"goal_id": str(goal.get("goal_id") or ""), "wait_kind": waiting["kind"]},
                created_at=now,
            )
            superseded = True
        return superseded

    def _goal_waiting_wake_reason(self, state: dict[str, Any], goal: dict[str, Any], waiting: dict[str, Any], *, now: str) -> str | None:
        session_id = str(goal.get("session_id") or "")
        kind = str(waiting.get("kind") or "")
        wait_log_seq = int(waiting.get("wake_log_seq") or 0)
        if kind == "deadline":
            try:
                deadline_passed = self._parse_utc(str(waiting.get("wait_until") or "")) <= self._parse_utc(now)
            except StoreError:
                deadline_passed = True
            if deadline_passed:
                return "bounded waiting deadline reached; GoalManager must review the SessionGoal again."
        if kind == "event":
            event_name = str(waiting.get("event_name") or "")
            for entry in self._session_log_after_cursor(state, session_id=session_id, cursor=wait_log_seq):
                if entry.get("kind") != "Message":
                    continue
                message = self._message_for_log_entry(state, entry)
                payload = message.get("payload") if isinstance(message, dict) else None
                events = payload.get("wake_events") if isinstance(payload, dict) else None
                if isinstance(events, str):
                    events = [events]
                if isinstance(events, list) and event_name in events:
                    return f"bounded waiting event {event_name} fired."
        if kind == "child":
            child_session_id = str(waiting.get("child_session_id") or "")
            for entry in self._session_log_after_cursor(state, session_id=session_id, cursor=wait_log_seq):
                if entry.get("kind") != "ChildGoalCompleted":
                    continue
                event = entry.get("event")
                if not isinstance(event, dict):
                    continue
                if not child_session_id or str(event.get("child_session_id") or "") == child_session_id:
                    return f"bounded waiting child SessionGoal {child_session_id or '(any child)'} completed."
        return None

    def _reconcile_dispatch_liveness(self, state: dict[str, Any]) -> bool:
        """Enforce that every Active/Incomplete Session has ready, running, or bounded-waiting work."""
        changed = False
        now = utc_now()
        acquired_runs = {
            str(run.get("run_id") or ""): run
            for run in state.setdefault("dispatch_runs", {}).values()
            if run.get("lease_state") == "acquired"
        }
        acquired_run_by_readiness = {
            str(run.get("readiness_id") or ""): run
            for run in acquired_runs.values()
            if str(run.get("readiness_id") or "")
        }
        repaired_by_session: dict[str, dict[str, list[str]]] = {}

        for run_id, run in acquired_runs.items():
            if not self._dispatch_lease_expired(run, now=now):
                continue
            session_id = str(run.get("session_id") or "")
            run["lease_state"] = "interrupted"
            run["interrupted_at"] = now
            run["interrupted_reason"] = "dispatch lease exceeded the Agent execution limit"
            run.pop("current_phase", None)
            repaired_by_session.setdefault(session_id, {"readiness_ids": [], "run_ids": []})["run_ids"].append(run_id)
            changed = True

        for readiness in state.setdefault("dispatch_readiness", []):
            if readiness.get("status") != "acquired":
                continue
            readiness_id = str(readiness.get("readiness_id") or "")
            matching_run = acquired_run_by_readiness.get(readiness_id)
            if matching_run and matching_run.get("lease_state") == "acquired":
                continue
            session_id = str(readiness.get("session_id") or "")
            readiness["status"] = "stale"
            readiness["stale_at"] = now
            readiness["stale_reason"] = "acquired readiness has no live acquired dispatch run"
            readiness.pop("acquired_at", None)
            repaired_by_session.setdefault(session_id, {"readiness_ids": [], "run_ids": []})[
                "readiness_ids"
            ].append(readiness_id)
            changed = True

        for session_id, repair in repaired_by_session.items():
            if session_id not in state.setdefault("sessions", {}):
                continue
            self._log_system_signal(
                state,
                session_id,
                signal_type="DispatchLivenessRecovered",
                body="AIze recovered dispatch ownership that no longer had a live Agent run. GoalManager must reassess the SessionGoal.",
                target_roles=[GOAL_MANAGER_ROLE],
                actor="dispatcher",
                data=repair,
                created_at=now,
            )
            changed = True

        if self._refresh_dispatch_readiness(state):
            changed = True

        live_goal_ids = {
            str(entry.get("goal_id") or "")
            for entry in state.setdefault("dispatch_readiness", [])
            if entry.get("status") in {"ready", "acquired"}
        }
        live_goal_ids.update(
            str(run.get("goal_id") or "")
            for run in state.setdefault("dispatch_runs", {}).values()
            if run.get("lease_state") == "acquired"
        )
        liveness_signals_added = False
        for goal in self._current_goals(state):
            goal_id = str(goal.get("goal_id") or "")
            session_id = str(goal.get("session_id") or "")
            session = state.setdefault("sessions", {}).get(session_id)
            if (
                not goal_id
                or goal.get("archived_at")
                or goal.get("completion_state") != "incomplete"
                or not session
                or session.get("active") is not True
                or self._unit_paused(state.setdefault("units", {}).get(str(session.get("unit_id") or "")))
                or goal_id in live_goal_ids
                or goal.get("waiting")
            ):
                continue
            self._log_system_signal(
                state,
                session_id,
                signal_type="SessionLivenessRecovery",
                body=(
                    "This Session is Active and its SessionGoal is incomplete, but no GoalManager or "
                    "WorkerAgent is ready or running. GoalManager must choose the next action."
                ),
                target_roles=[GOAL_MANAGER_ROLE],
                actor="dispatcher",
                data={"goal_id": goal_id},
                created_at=now,
            )
            live_goal_ids.add(goal_id)
            liveness_signals_added = True
            changed = True
        if liveness_signals_added and self._refresh_dispatch_readiness(state):
            changed = True
        return changed

    @staticmethod
    def _dispatch_lease_expired(run: dict[str, Any], *, now: str) -> bool:
        acquired_at = str(run.get("lease_acquired_at") or run.get("created_at") or "")
        if not acquired_at:
            return True
        try:
            acquired = datetime.fromisoformat(acquired_at.replace("Z", "+00:00"))
            current = datetime.fromisoformat(now.replace("Z", "+00:00"))
        except ValueError:
            return True
        if acquired.tzinfo is None:
            acquired = acquired.replace(tzinfo=UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        return current >= acquired + timedelta(seconds=DISPATCH_LEASE_RECOVERY_SECONDS)

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
