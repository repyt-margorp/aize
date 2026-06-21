from __future__ import annotations

import json
from typing import Any

from .model import Goal, Session, Unit, new_id, utc_now
from .store_defs import (
    AGENT_ROLES,
    DEFAULT_AGENT_PROVIDER,
    DEFAULT_SESSION_CAPABILITIES,
    DISPATCH_PRIORITY_GOAL,
    GOAL_MANAGER_ROLE,
    ROOT_SESSION_ID,
    WORKER_AGENT_ROLE,
    StoreError,
    normalize_endpoint,
)


class SessionGoalMixin:
    def _ensure_session_metadata(self, state: dict[str, Any]) -> bool:
        changed = False
        for session in state.get("sessions", {}).values():
            if session.get("capabilities") != DEFAULT_SESSION_CAPABILITIES:
                session["capabilities"] = json.loads(json.dumps(DEFAULT_SESSION_CAPABILITIES, ensure_ascii=False))
                changed = True
        return changed

    def create_unit(self, unit_id: str, *, instance_policy: str = "multi") -> Unit:
        state = self.load()
        if instance_policy not in {"multi", "singleton"}:
            raise StoreError(f"unsupported instance policy: {instance_policy}")
        units = state["units"]
        if unit_id in units:
            raise StoreError(f"unit already exists: {unit_id}")
        unit = Unit(unit_id=unit_id, created_at=utc_now(), instance_policy=instance_policy)
        units[unit_id] = unit.to_dict()
        self.save(state)
        return unit

    def set_agent_provider(self, role: str, *, provider: str) -> dict[str, Any]:
        normalized_role = self._normalize_agent_role(role)
        if normalized_role not in AGENT_ROLES:
            raise StoreError(f"unsupported agent role: {role}")
        normalized_provider = provider.strip().lower()
        if normalized_provider not in {"local", "codex", "claude", "remote-aize"}:
            raise StoreError(f"unsupported agent provider: {provider}")
        if normalized_role == GOAL_MANAGER_ROLE and normalized_provider == "remote-aize":
            raise StoreError("GoalManager provider must be local to this PC")
        state = self.load()
        profile = state["agent_profiles"].setdefault(
            normalized_role,
            {
                "role": normalized_role,
                "created_at": utc_now(),
            },
        )
        profile["provider"] = normalized_provider
        profile["default_assignment"] = False
        profile["status"] = "active"
        profile["updated_at"] = utc_now()
        self.save(state)
        return dict(profile)

    def agent_profiles(self) -> list[dict[str, Any]]:
        state = self.load()
        return sorted(state["agent_profiles"].values(), key=lambda item: item["role"])

    def set_session_active(self, session_id: str, *, active: bool) -> dict[str, Any]:
        state = self.load()
        session = state["sessions"].get(session_id)
        if not session:
            raise StoreError(f"unknown session: {session_id}")
        now = utc_now()
        session["active"] = active
        session["updated_at"] = now
        if active:
            for goal in state["goals"].values():
                if str(goal.get("session_id") or "") != session_id:
                    continue
                if goal.get("archived_at"):
                    continue
                if goal.get("completion_state") != "incomplete":
                    continue
                self._enqueue_dispatch(
                    state,
                    goal,
                    priority=DISPATCH_PRIORITY_GOAL,
                    reason="Session activated with incomplete Goal.",
                    queued_at=now,
                )
        self.save(state)
        return dict(session)

    def _normalize_agent_role(self, role: str) -> str:
        normalized = role.strip()
        aliases = {
            "goalmanager": GOAL_MANAGER_ROLE,
            "GoalManager": GOAL_MANAGER_ROLE,
            "workeragent": WORKER_AGENT_ROLE,
            "WorkerAgent": WORKER_AGENT_ROLE,
            "worker": WORKER_AGENT_ROLE,
        }
        return aliases.get(normalized, aliases.get(normalized.lower(), normalized))

    def create_session(
        self,
        session_id: str,
        *,
        unit_id: str | None = None,
        parent_session_ids: list[str] | None = None,
    ) -> Session:
        state = self.load()
        normalized_unit_id = str(unit_id or "").strip() or None
        if normalized_unit_id and normalized_unit_id not in state["units"]:
            raise StoreError(f"unknown unit: {normalized_unit_id}")
        sessions = state["sessions"]
        if session_id in sessions:
            raise StoreError(f"session already exists: {session_id}")
        normalized_parent_ids = parent_session_ids or [ROOT_SESSION_ID]
        if session_id != ROOT_SESSION_ID:
            for parent_session_id in normalized_parent_ids:
                if parent_session_id not in sessions:
                    raise StoreError(f"unknown parent session: {parent_session_id}")
        unit = state["units"].get(normalized_unit_id) if normalized_unit_id else None
        if unit and unit.get("instance_policy") == "singleton":
            singleton_session_id = unit.get("singleton_session_id")
            if singleton_session_id and singleton_session_id in sessions:
                raise StoreError(f"unit is singleton and already has session: {singleton_session_id}")
        now = utc_now()
        session = Session(
            session_id=session_id,
            unit_id=normalized_unit_id,
            created_at=now,
            updated_at=now,
            title=session_id,
        )
        sessions[session_id] = session.to_dict()
        if unit and unit.get("instance_policy") == "singleton":
            unit["singleton_session_id"] = session_id
            sessions[session_id]["singleton"] = True
        sessions[session_id]["capabilities"] = json.loads(json.dumps(DEFAULT_SESSION_CAPABILITIES, ensure_ascii=False))
        if session_id != ROOT_SESSION_ID:
            for parent_session_id in normalized_parent_ids:
                self._link_sessions_in_state(state, parent_session_id, session_id)
        self.save(state)
        return session

    def start_goal_session(
        self,
        session_id: str,
        *,
        unit_id: str | None = None,
        parent_session_ids: list[str],
        label: str,
        body: str = "",
        created_by: str,
    ) -> dict[str, Any]:
        session_title = label.strip()
        if not session_title:
            raise StoreError("session label is required")
        goal_body = str(body or "").strip() or session_title
        state = self.load()
        if created_by not in state["accounts"]:
            raise StoreError(f"unknown account: {created_by}")
        normalized_unit_id = str(unit_id or "").strip() or None
        if normalized_unit_id and normalized_unit_id not in state["units"]:
            raise StoreError(f"unknown unit: {normalized_unit_id}")
        if session_id in state["sessions"]:
            raise StoreError(f"session already exists: {session_id}")
        unit = state["units"].get(normalized_unit_id) if normalized_unit_id else None
        if unit and unit.get("instance_policy") == "singleton":
            singleton_session_id = unit.get("singleton_session_id")
            if singleton_session_id and singleton_session_id in state["sessions"]:
                raise StoreError(f"unit is singleton and already has session: {singleton_session_id}")
        normalized_parent_ids = parent_session_ids or [ROOT_SESSION_ID]
        for parent_session_id in normalized_parent_ids:
            if parent_session_id not in state["sessions"]:
                raise StoreError(f"unknown parent session: {parent_session_id}")

        now = utc_now()
        session = Session(
            session_id=session_id,
            unit_id=normalized_unit_id,
            created_at=now,
            updated_at=now,
            title=session_title,
        )
        state["sessions"][session_id] = session.to_dict()
        state["sessions"][session_id]["capabilities"] = json.loads(json.dumps(DEFAULT_SESSION_CAPABILITIES, ensure_ascii=False))
        for parent_session_id in normalized_parent_ids:
            self._link_sessions_in_state(state, parent_session_id, session_id)

        goal = Goal(
            goal_id=new_id("goal"),
            session_id=session_id,
            body=goal_body,
            created_by=created_by,
            created_at=now,
        )
        goal_dict = goal.to_dict()
        state["goals"][goal.goal_id] = goal_dict
        self._set_goal_completion_state(
            state,
            goal_dict,
            "incomplete",
            reason="Goal session created.",
            actor="system",
            priority=DISPATCH_PRIORITY_GOAL,
        )
        self.save(state)
        return {"session": dict(state["sessions"][session_id]), "goal": goal_dict}

    def update_goal(
        self,
        session_id: str,
        *,
        body: str,
        created_by: str,
    ) -> dict[str, Any]:
        normalized_body = str(body or "").strip()
        if not normalized_body:
            raise StoreError("goal body is required")
        state = self.load()
        if session_id not in state["sessions"]:
            raise StoreError(f"unknown session: {session_id}")
        if created_by not in state["accounts"]:
            raise StoreError(f"unknown account: {created_by}")
        now = utc_now()
        goal_dict = self._current_goal_for_session(state, session_id)
        reason = "SessionGoal updated."
        if goal_dict:
            goal_dict["body"] = normalized_body
            goal_dict["created_by"] = created_by
            goal_dict["updated_at"] = now
        else:
            goal = Goal(
                goal_id=new_id("goal"),
                session_id=session_id,
                body=normalized_body,
                created_by=created_by,
                created_at=now,
            )
            goal_dict = goal.to_dict()
            state["goals"][goal.goal_id] = goal_dict
            reason = "SessionGoal created."
        state["sessions"][session_id]["updated_at"] = now
        self._set_goal_completion_state(
            state,
            goal_dict,
            "incomplete",
            reason=reason,
            actor="system",
            priority=DISPATCH_PRIORITY_GOAL,
        )
        self.save(state)
        return goal_dict

    def _set_goal_completion_state(
        self,
        state: dict[str, Any],
        goal: dict[str, Any],
        completion_state: str,
        *,
        reason: str,
        actor: str,
        run_id: str | None = None,
        priority: int = DISPATCH_PRIORITY_GOAL,
        enqueue_on_incomplete: bool = True,
        available_after: str | None = None,
        trigger_message_id: str | None = None,
    ) -> dict[str, Any]:
        if completion_state not in {"complete", "incomplete"}:
            raise StoreError(f"unsupported goal completion state: {completion_state}")
        now = utc_now()
        previous_state = str(goal.get("completion_state") or "incomplete")
        goal["completion_state"] = completion_state
        goal["completion_reason"] = reason
        goal["completion_reason_updated_at"] = now
        if completion_state == "complete":
            goal["completed_at"] = now
            self._resolve_dispatch_queue_entries(state, str(goal.get("goal_id") or ""), resolved_at=now)
        else:
            goal.pop("completed_at", None)
            goal["last_incomplete_at"] = now
            if enqueue_on_incomplete:
                self._enqueue_dispatch(
                    state,
                    goal,
                    priority=priority,
                    reason=reason,
                    queued_at=now,
                    available_after=available_after,
                    trigger_message_id=trigger_message_id,
                )
        session = state["sessions"].get(str(goal.get("session_id") or ""))
        if session:
            session["updated_at"] = now
        transition = {
            "previous_state": previous_state,
            "completion_state": completion_state,
            "reason": reason,
            "actor": normalize_endpoint(actor, session_id=str(goal.get("session_id") or "")),
            "run_id": run_id,
            "created_at": now,
        }
        if run_id:
            transition["run_id"] = run_id
        goal.setdefault("state_transitions", []).append(transition)
        return transition

    def _current_goal_for_session(self, state: dict[str, Any], session_id: str) -> dict[str, Any] | None:
        session_goals = sorted(
            [
                goal
                for goal in state.get("goals", {}).values()
                if str(goal.get("session_id") or "") == session_id and not goal.get("archived_at")
            ],
            key=lambda item: (str(item.get("created_at") or ""), str(item.get("goal_id") or "")),
        )
        return session_goals[-1] if session_goals else None

    def _current_goals(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        by_session: dict[str, dict[str, Any]] = {}
        for goal in sorted(
            [
                goal
                for goal in state.get("goals", {}).values()
                if not goal.get("archived_at")
            ],
            key=lambda item: (str(item.get("created_at") or ""), str(item.get("goal_id") or "")),
        ):
            session_id = str(goal.get("session_id") or "")
            if session_id:
                by_session[session_id] = goal
        return [goal for session_id, goal in sorted(by_session.items())]

    def _mark_session_reprocess_needed(
        self,
        state: dict[str, Any],
        *,
        session_id: str,
        actor: str,
        reason: str,
        priority: int,
        created_at: str,
        trigger_message_id: str | None = None,
    ) -> dict[str, Any]:
        target_goal = self._current_goal_for_session(state, session_id)
        if not target_goal:
            target_goal = Goal(
                goal_id=new_id("goal"),
                session_id=session_id,
                body="Reply to user input sent to this Session.",
                created_by=actor,
                created_at=created_at,
            ).to_dict()
            state["goals"][target_goal["goal_id"]] = target_goal
        self._set_goal_completion_state(
            state,
            target_goal,
            "incomplete",
            reason=reason,
            actor=actor,
            priority=priority,
            trigger_message_id=trigger_message_id,
        )
        return target_goal

    def link_sessions(self, parent_session_id: str, child_session_id: str) -> dict[str, Any]:
        state = self.load()
        edge = self._link_sessions_in_state(state, parent_session_id, child_session_id)
        self.save(state)
        return edge

    def _link_sessions_in_state(
        self,
        state: dict[str, Any],
        parent_session_id: str,
        child_session_id: str,
    ) -> dict[str, Any]:
        sessions = state["sessions"]
        if parent_session_id not in sessions:
            raise StoreError(f"unknown parent session: {parent_session_id}")
        if child_session_id not in sessions:
            raise StoreError(f"unknown child session: {child_session_id}")
        if parent_session_id == child_session_id:
            raise StoreError("session graph cannot contain self edges")

        edges = state["session_edges"]
        for edge in edges:
            if (
                edge.get("parent_session_id") == parent_session_id
                and edge.get("child_session_id") == child_session_id
            ):
                return dict(edge)

        if self._has_path(edges, child_session_id, parent_session_id):
            raise StoreError(f"link would create a cycle: {parent_session_id} -> {child_session_id}")

        edge = {
            "parent_session_id": parent_session_id,
            "child_session_id": child_session_id,
            "created_at": utc_now(),
        }
        edges.append(edge)
        return dict(edge)

    def _has_path(self, edges: list[dict[str, Any]], start: str, target: str) -> bool:
        children_by_parent: dict[str, list[str]] = {}
        for edge in edges:
            children_by_parent.setdefault(str(edge.get("parent_session_id")), []).append(
                str(edge.get("child_session_id"))
            )
        stack = [start]
        seen: set[str] = set()
        while stack:
            session_id = stack.pop()
            if session_id == target:
                return True
            if session_id in seen:
                continue
            seen.add(session_id)
            stack.extend(children_by_parent.get(session_id, []))
        return False
