from __future__ import annotations

from typing import Any

from kernel.model import new_id, utc_now
from kernel.defs import normalize_endpoint
from goal.defs import GOAL_MANAGER_ROLE, WORKER_AGENT_ROLE


class GoalLogMixin:
    def _ensure_session_log_defaults(self, state: dict[str, Any]) -> bool:
        changed = False
        for session_id, session in state.setdefault("sessions", {}).items():
            if self._ensure_role_cursors(session):
                changed = True
            if self._sync_session_current_state(state, session_id):
                changed = True

        return changed

    def _reconcile_state_from_session_logs(self, state: dict[str, Any]) -> bool:
        changed = False
        for session_id in self._all_session_log_ids(state):
            session = state.setdefault("sessions", {}).get(session_id)
            if not session:
                continue
            entries = self._session_log_entries(state, session_id)
            latest_seq = int(entries[-1].get("seq") or 0) if entries else 0
            self._ensure_role_cursors(session)
            for role, cursor in list(session.setdefault("role_cursors", {}).items()):
                normalized = min(max(0, int(cursor or 0)), latest_seq)
                if normalized != cursor:
                    session["role_cursors"][role] = normalized
                    changed = True
            goal = self._current_goal_for_session(state, session_id)
            for entry in entries:
                kind = str(entry.get("kind") or "")
                event = entry.get("event")
                created_at = str(entry.get("created_at") or "")
                if kind == "SessionActiveChanged" and isinstance(event, dict):
                    active = bool(event.get("active"))
                    if session.get("active") is not active:
                        session["active"] = active
                        changed = True
                elif kind == "GoalStateChanged" and isinstance(event, dict) and goal:
                    completion_state = str(event.get("completion_state") or "")
                    if completion_state not in {"complete", "incomplete"}:
                        continue
                    reason = str(event.get("reason") or "")
                    if goal.get("completion_state") != completion_state:
                        goal["completion_state"] = completion_state
                        changed = True
                    if goal.get("completion_reason") != reason:
                        goal["completion_reason"] = reason
                        changed = True
                    goal["completion_reason_updated_at"] = created_at
                    if completion_state == "complete":
                        goal["completed_at"] = created_at
                        goal.pop("last_incomplete_at", None)
                    else:
                        goal["last_incomplete_at"] = created_at
                        goal.pop("completed_at", None)
                elif kind == "Message" and goal:
                    message = self._message_for_log_entry(state, entry)
                    payload = message.get("payload") if isinstance(message, dict) else None
                    if not isinstance(payload, dict) or payload.get("user_input") is not True:
                        continue
                    if goal.get("completion_state") == "complete":
                        goal["completion_state"] = "incomplete"
                        goal["completion_reason"] = (
                            f"UserInput message {message.get('message_id')} requires Session processing."
                        )
                        goal["completion_reason_updated_at"] = created_at
                        goal["last_incomplete_at"] = created_at
                        goal.pop("completed_at", None)
                        changed = True
                if created_at and str(session.get("updated_at") or "") < created_at:
                    session["updated_at"] = created_at
                    changed = True
            if self._sync_session_current_state(state, session_id):
                changed = True
        return changed

    def _ensure_role_cursors(self, session: dict[str, Any]) -> bool:
        cursors = session.setdefault("role_cursors", {})
        changed = False
        for role in (GOAL_MANAGER_ROLE, WORKER_AGENT_ROLE):
            if role not in cursors:
                cursors[role] = 0
                changed = True
        return changed

    def _sync_session_current_state(self, state: dict[str, Any], session_id: str) -> bool:
        session = state.setdefault("sessions", {}).get(session_id)
        if not session:
            return False
        goal = self._current_goal_for_session(state, session_id)
        current_state = {
            "active": bool(session.get("active", True)),
            "goal_id": str(goal.get("goal_id") or "") if goal else "",
            "goal_completion_state": str(goal.get("completion_state") or "") if goal else "",
        }
        if session.get("current_state") == current_state:
            return False
        session["current_state"] = current_state
        return True

    def _log_goal_state_transition(
        self,
        state: dict[str, Any],
        goal: dict[str, Any],
        transition: dict[str, Any],
    ) -> None:
        session_id = str(goal.get("session_id") or "")
        if not session_id:
            return
        self._append_session_log_entry(
            state,
            session_id,
            kind="GoalStateChanged",
            created_at=str(transition.get("created_at") or utc_now()),
            event=dict(transition),
            event_key=self._goal_state_event_key(goal, transition),
        )
        self._sync_session_current_state(state, session_id)

    def _log_goal_body_update(
        self,
        state: dict[str, Any],
        session_id: str,
        *,
        goal_id: str,
        previous_body: str,
        body: str,
        actor: str,
        created_at: str,
    ) -> None:
        self._append_session_log_entry(
            state,
            session_id,
            kind="GoalBodyUpdated",
            created_at=created_at,
            event={
                "goal_id": goal_id,
                "previous_body": previous_body,
                "body": body,
                "actor": normalize_endpoint(actor, session_id=session_id),
                "created_at": created_at,
            },
            event_key=f"goal-body:{goal_id}:{created_at}:{new_id('revision')}",
        )
        self._sync_session_current_state(state, session_id)

    def _log_child_goal_completed_for_parents(
        self,
        state: dict[str, Any],
        goal: dict[str, Any],
        transition: dict[str, Any],
    ) -> None:
        child_session_id = str(goal.get("session_id") or "")
        goal_id = str(goal.get("goal_id") or "")
        completed_at = str(transition.get("created_at") or utc_now())
        if not child_session_id or not goal_id:
            return
        for parent_session_id in self._parent_session_ids(state, child_session_id):
            parent_session = state.setdefault("sessions", {}).get(parent_session_id)
            if not parent_session:
                continue
            self._append_session_log_entry(
                state,
                parent_session_id,
                kind="ChildGoalCompleted",
                created_at=completed_at,
                event={
                    "child_session_id": child_session_id,
                    "child_goal_id": goal_id,
                    "child_goal_body": str(goal.get("body") or ""),
                    "completion_state": "complete",
                    "completion_reason": str(transition.get("reason") or ""),
                    "actor": str(transition.get("actor") or ""),
                    "run_id": str(transition.get("run_id") or ""),
                    "created_at": completed_at,
                },
                event_key=f"child-goal-completed:{parent_session_id}:{goal_id}:{completed_at}",
            )
            parent_session["updated_at"] = completed_at

    def _log_session_active_change(
        self,
        state: dict[str, Any],
        session_id: str,
        *,
        previous_active: bool,
        active: bool,
        actor: str,
        created_at: str,
    ) -> None:
        self._append_session_log_entry(
            state,
            session_id,
            kind="SessionActiveChanged",
            created_at=created_at,
            event={
                "previous_active": previous_active,
                "active": active,
                "actor": normalize_endpoint(actor, session_id=session_id),
                "created_at": created_at,
            },
            event_key=f"session-active:{session_id}:{created_at}:{previous_active}->{active}",
        )
        self._sync_session_current_state(state, session_id)

    def _log_system_signal(
        self,
        state: dict[str, Any],
        session_id: str,
        *,
        signal_type: str,
        body: str,
        target_roles: list[str],
        actor: str = "system",
        run_id: str | None = None,
        data: dict[str, Any] | None = None,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        normalized_roles = [role for role in target_roles if role in {GOAL_MANAGER_ROLE, WORKER_AGENT_ROLE}]
        if not normalized_roles:
            normalized_roles = [GOAL_MANAGER_ROLE]
        timestamp = created_at or utc_now()
        signal = {
            "signal_id": new_id("sig"),
            "signal_type": str(signal_type or "system").strip() or "system",
            "body": str(body or "").strip(),
            "target_roles": normalized_roles,
            "actor": normalize_endpoint(actor, session_id=session_id),
            "run_id": str(run_id or ""),
            "created_at": timestamp,
            "data": dict(data or {}),
        }
        return self._append_session_log_entry(
            state,
            session_id,
            kind="SystemSignal",
            created_at=timestamp,
            event=signal,
            event_key=f"system-signal:{signal['signal_id']}",
        )

    def _goal_state_event_key(self, goal: dict[str, Any], transition: dict[str, Any]) -> str:
        return ":".join(
            [
                "goal-state",
                str(goal.get("goal_id") or ""),
                str(transition.get("created_at") or ""),
                str(transition.get("completion_state") or ""),
                str(transition.get("run_id") or ""),
                str(transition.get("actor") or ""),
            ]
        )

    def _session_log_entries_after_cursor(
        self,
        state: dict[str, Any],
        *,
        session_id: str,
        role: str,
    ) -> list[dict[str, Any]]:
        session = state.setdefault("sessions", {}).get(session_id, {})
        self._ensure_role_cursors(session)
        cursor = int(session.setdefault("role_cursors", {}).get(role) or 0)
        return self._session_log_after_cursor(state, session_id=session_id, cursor=cursor)

    def _set_role_cursor(self, state: dict[str, Any], *, session_id: str, role: str, seq: int) -> None:
        session = state.setdefault("sessions", {}).get(session_id)
        if not session:
            return
        self._ensure_role_cursors(session)
        self._set_session_log_cursor(state, session_id=session_id, cursor_key=role, seq=seq)

    def record_runtime_recovery_signals(self, context: str) -> list[dict[str, Any]]:
        with self._state_lock():
            state = self.load()
            timestamp = utc_now()
            signals: list[dict[str, Any]] = []
            current_active_incomplete_goals: dict[str, dict[str, Any]] = {}
            for goal in self._current_goals(state):
                session_id = str(goal.get("session_id") or "")
                session = state.get("sessions", {}).get(session_id)
                if (
                    goal.get("completion_state") == "incomplete"
                    and not goal.get("archived_at")
                    and session
                    and session.get("active") is True
                ):
                    current_active_incomplete_goals[str(goal.get("goal_id") or "")] = goal

            for run in state.get("dispatch_runs", {}).values():
                if run.get("lease_state") != "acquired":
                    continue
                if str(run.get("goal_id") or "") in current_active_incomplete_goals:
                    continue
                run["lease_state"] = "interrupted"
                run["interrupted_at"] = timestamp
                run["interrupted_reason"] = "runtime recovered stale acquired run for non-dispatchable goal"
                run.pop("current_phase", None)

            for readiness in state.setdefault("dispatch_readiness", []):
                if readiness.get("status") != "acquired":
                    continue
                if str(readiness.get("goal_id") or "") in current_active_incomplete_goals:
                    continue
                readiness["status"] = "stale"
                readiness["stale_at"] = timestamp
                readiness["stale_reason"] = "runtime recovered stale acquired readiness for non-dispatchable goal"
                readiness.pop("acquired_at", None)

            for goal in current_active_incomplete_goals.values():
                if goal.get("completion_state") != "incomplete" or goal.get("archived_at"):
                    continue
                session_id = str(goal.get("session_id") or "")
                session = state.get("sessions", {}).get(session_id)
                if not session or session.get("active") is not True:
                    continue
                interrupted_roles: list[str] = []
                interrupted_readiness_ids: set[str] = set()
                for run in state.get("dispatch_runs", {}).values():
                    if str(run.get("session_id") or "") != session_id or run.get("lease_state") != "acquired":
                        continue
                    role = str(run.get("role") or run.get("current_phase") or "")
                    if role in {GOAL_MANAGER_ROLE, WORKER_AGENT_ROLE} and role not in interrupted_roles:
                        interrupted_roles.append(role)
                    readiness_id = str(run.get("readiness_id") or "")
                    if readiness_id:
                        interrupted_readiness_ids.add(readiness_id)
                    run["lease_state"] = "interrupted"
                    run["interrupted_at"] = timestamp
                    run.pop("current_phase", None)
                for readiness in state.setdefault("dispatch_readiness", []):
                    if str(readiness.get("session_id") or "") != session_id or readiness.get("status") != "acquired":
                        continue
                    readiness_id = str(readiness.get("readiness_id") or "")
                    if readiness_id in interrupted_readiness_ids:
                        readiness["status"] = "ready"
                        readiness["recovered_at"] = timestamp
                    else:
                        readiness["status"] = "stale"
                        readiness["stale_at"] = timestamp
                        readiness["stale_reason"] = (
                            "runtime recovered acquired readiness without a matching acquired run"
                        )
                    readiness.pop("acquired_at", None)
                target_roles = [GOAL_MANAGER_ROLE]
                for role in interrupted_roles:
                    if role not in target_roles:
                        target_roles.append(role)
                signal = self._log_system_signal(
                    state,
                    session_id,
                    signal_type="RuntimeRecovered",
                    body=(
                        context
                        or "AIze runtime started or restarted. Continue the active incomplete SessionGoal from persisted SessionLog state."
                    ),
                    target_roles=target_roles,
                    actor="runtime",
                    data={
                        "goal_id": str(goal.get("goal_id") or ""),
                        "interrupted_roles": interrupted_roles,
                    },
                    created_at=timestamp,
                )
                signals.append(dict(signal))
            self.save(state)
            return signals
