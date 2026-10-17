from __future__ import annotations

from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import Any

from kernel.model import utc_now
from kernel.defs import StoreError


class QueryMixin:
    def dispatch_lot_size(self) -> int:
        state = self.load()
        return max(1, int(state.setdefault("runtime_settings", {}).get("dispatch_lot_size") or 1))

    def set_dispatch_lot_size(self, lot_size: int) -> dict[str, Any]:
        normalized_size = int(lot_size)
        if normalized_size < 1:
            raise StoreError("dispatch lot size must be positive")
        with self._state_lock():
            state = self.load()
            state.setdefault("runtime_settings", {})["dispatch_lot_size"] = normalized_size
            self.save(state)
        return {"dispatch_lot_size": normalized_size}

    def status(self) -> dict[str, Any]:
        state = self.load()
        self._refresh_dispatch_readiness(state)
        liveness = self._dispatch_liveness_snapshot(state)
        message_count = self.storage.session_log_stats()["message_count"]
        active_sessions = [session for session in state["sessions"].values() if session.get("active") is True]
        inactive_sessions = [session for session in state["sessions"].values() if session.get("active") is not True]
        current_goals = self._current_goals(state)
        incomplete_goals = [goal for goal in current_goals if goal.get("completion_state") == "incomplete"]
        complete_goals = [goal for goal in current_goals if goal.get("completion_state") == "complete"]
        active_incomplete_goals = [
            goal
            for goal in incomplete_goals
            if state["sessions"].get(str(goal.get("session_id") or ""), {}).get("active") is True
        ]
        acquired_dispatch_leases = [
            run for run in state["dispatch_runs"].values() if run.get("lease_state") == "acquired"
        ]
        ready_dispatch_entries = [
            entry for entry in state.get("dispatch_readiness", []) if entry.get("status") == "ready"
        ]
        acquired_dispatch_readiness = [
            entry for entry in state.get("dispatch_readiness", []) if entry.get("status") == "acquired"
        ]
        return {
            "version": state["version"],
            "created_at": state["created_at"],
            "unit_count": len(state["units"]),
            "session_count": len(state["sessions"]),
            "active_session_count": len(active_sessions),
            "inactive_session_count": len(inactive_sessions),
            "session_edge_count": len(state["session_edges"]),
            "user_count": len(state["users"]),
            "goal_count": len(current_goals),
            "incomplete_goal_count": len(incomplete_goals),
            "complete_goal_count": len(complete_goals),
            "active_incomplete_goal_count": len(active_incomplete_goals),
            "dispatch_run_count": len(state["dispatch_runs"]),
            "acquired_dispatch_lease_count": len(acquired_dispatch_leases),
            "dispatch_readiness_count": len(state.get("dispatch_readiness", [])),
            "ready_dispatch_count": len(ready_dispatch_entries),
            "acquired_dispatch_readiness_count": len(acquired_dispatch_readiness),
            "dispatch_liveness_gap_count": liveness["gap_count"],
            "orphaned_acquired_readiness_count": liveness["orphaned_acquired_readiness_count"],
            "expired_acquired_dispatch_lease_count": liveness["expired_acquired_dispatch_lease_count"],
            "dispatch_lot_size": max(1, int(state.setdefault("runtime_settings", {}).get("dispatch_lot_size") or 1)),
            "agent_profile_count": len(state["agent_profiles"]),
            "agent_thread_count": len(state["agent_threads"]),
            "message_count": message_count,
            "endpoint_cursor_count": len(state.get("endpoint_cursors", {})),
        }

    def dispatch_liveness(self) -> dict[str, Any]:
        state = self.load()
        self._refresh_dispatch_readiness(state)
        return self._dispatch_liveness_snapshot(state)

    def _dispatch_liveness_snapshot(self, state: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        acquired_runs = [
            run
            for run in state.setdefault("dispatch_runs", {}).values()
            if run.get("lease_state") == "acquired"
        ]
        live_runs = [run for run in acquired_runs if not self._dispatch_lease_expired(run, now=now)]
        live_run_by_readiness = {
            str(run.get("readiness_id") or ""): run
            for run in live_runs
            if str(run.get("readiness_id") or "")
        }
        orphaned_readiness = [
            entry
            for entry in state.setdefault("dispatch_readiness", [])
            if entry.get("status") == "acquired"
            and str(entry.get("readiness_id") or "") not in live_run_by_readiness
        ]
        ready_roles_by_goal: dict[str, set[str]] = {}
        for entry in state.setdefault("dispatch_readiness", []):
            status = str(entry.get("status") or "")
            if status != "ready":
                continue
            ready_roles_by_goal.setdefault(str(entry.get("goal_id") or ""), set()).add(
                str(entry.get("role") or "")
            )
        running_roles_by_goal: dict[str, set[str]] = {}
        for run in live_runs:
            running_roles_by_goal.setdefault(str(run.get("goal_id") or ""), set()).add(
                str(run.get("role") or run.get("current_phase") or "")
            )

        sessions: list[dict[str, Any]] = []
        for goal in self._current_goals(state):
            session_id = str(goal.get("session_id") or "")
            session = state.setdefault("sessions", {}).get(session_id)
            if (
                goal.get("archived_at")
                or goal.get("completion_state") != "incomplete"
                or not session
                or session.get("active") is not True
                or self._unit_paused(state.setdefault("units", {}).get(str(session.get("unit_id") or "")))
            ):
                continue
            goal_id = str(goal.get("goal_id") or "")
            ready_roles = sorted(role for role in ready_roles_by_goal.get(goal_id, set()) if role)
            running_roles = sorted(role for role in running_roles_by_goal.get(goal_id, set()) if role)
            waiting = goal.get("waiting") is not None
            sessions.append(
                {
                    "session_id": session_id,
                    "goal_id": goal_id,
                    "ready_roles": ready_roles,
                    "running_roles": running_roles,
                    "waiting": waiting,
                    "covered": bool(ready_roles or running_roles) or waiting,
                }
            )
        sessions.sort(key=lambda item: str(item["session_id"]))
        return {
            "active_incomplete_session_count": len(sessions),
            "covered_count": sum(1 for item in sessions if item["covered"]),
            "waiting_count": sum(1 for item in sessions if item["waiting"]),
            "gap_count": sum(1 for item in sessions if not item["covered"]),
            "orphaned_acquired_readiness_count": len(orphaned_readiness),
            "orphaned_acquired_readiness_ids": sorted(
                str(entry.get("readiness_id") or "") for entry in orphaned_readiness
            ),
            "expired_acquired_dispatch_lease_count": len(acquired_runs) - len(live_runs),
            "expired_acquired_dispatch_run_ids": sorted(
                str(run.get("run_id") or "") for run in acquired_runs if run not in live_runs
            ),
            "sessions": sessions,
        }

    def goals(self, session_id: str | None = None) -> list[dict[str, Any]]:
        state = self.load()
        goals = self._current_goals(state)
        if session_id:
            goals = [goal for goal in goals if goal.get("session_id") == session_id]
        return sorted(goals, key=lambda item: (item["created_at"], item["goal_id"]))

    def dispatch_runs(
        self,
        session_id: str | None = None,
        *,
        include_output: bool = True,
    ) -> list[dict[str, Any]]:
        state = self.load()
        runs = list(state["dispatch_runs"].values())
        if session_id:
            runs = [run for run in runs if run.get("session_id") == session_id]
        if include_output:
            runs = self.storage.hydrate_dispatch_runs(runs)
        return sorted(runs, key=lambda item: (item["created_at"], item["run_id"]))

    def dispatch_readiness(self, session_id: str | None = None) -> list[dict[str, Any]]:
        state = self.load()
        self._refresh_dispatch_readiness(state)
        indexed_entries = list(enumerate(state.get("dispatch_readiness", [])))
        if session_id:
            indexed_entries = [
                (index, entry)
                for index, entry in indexed_entries
                if entry.get("session_id") == session_id
            ]
        sorted_entries = sorted(
            indexed_entries,
            key=lambda item: (
                str(item[1].get("status") or ""),
                str(item[1].get("first_ready_at") or ""),
                str(item[1].get("session_id") or ""),
                str(item[1].get("role") or ""),
                item[0],
            ),
        )
        return [entry for _, entry in sorted_entries]

    def agent_threads(self, session_id: str | None = None) -> list[dict[str, Any]]:
        state = self.load()
        threads = list(state["agent_threads"].values())
        if session_id:
            threads = [thread for thread in threads if thread.get("session_id") == session_id]
        threads = self.storage.hydrate_agent_threads(threads)
        return sorted(threads, key=lambda item: (item["session_id"], item["role"]))

    def session(self, session_id: str) -> dict[str, Any]:
        state = self.load()
        session = state["sessions"].get(session_id)
        if not session:
            raise StoreError(f"unknown session: {session_id}")
        return dict(session)

    def units(self) -> list[dict[str, Any]]:
        state = self.load()
        return sorted(state["units"].values(), key=lambda item: item["unit_id"])

    def sessions(self) -> list[dict[str, Any]]:
        state = self.load()
        return sorted(state["sessions"].values(), key=lambda item: item["session_id"])

    def session_graph(self) -> dict[str, Any]:
        state = self.load()
        return {
            "sessions": self.sessions(),
            "edges": sorted(
                state["session_edges"],
                key=lambda item: (item["parent_session_id"], item["child_session_id"]),
            ),
        }

    def active_incomplete_session_graph(
        self,
        *,
        updated_within_hours: float = 24,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        hours = float(updated_within_hours)
        if not isfinite(hours) or hours <= 0:
            raise StoreError("graph hours must be positive")

        state = self.load()
        current_goals = {
            str(goal.get("session_id") or ""): goal
            for goal in self._current_goals(state)
        }
        try:
            cutoff = (now or datetime.now(UTC)) - timedelta(hours=hours)
        except OverflowError as exc:
            raise StoreError("graph hours are too large") from exc
        sessions: list[dict[str, Any]] = []
        selected_ids: set[str] = set()
        for session in state["sessions"].values():
            session_id = str(session.get("session_id") or "")
            goal = current_goals.get(session_id)
            if session.get("active") is not True or not goal:
                continue
            if goal.get("completion_state") != "incomplete":
                continue
            try:
                updated_at = self._parse_utc(str(session.get("updated_at") or ""))
            except (StoreError, TypeError, ValueError):
                continue
            if updated_at < cutoff:
                continue
            selected_ids.add(session_id)
            sessions.append(dict(session))

        return {
            "sessions": sorted(sessions, key=lambda item: str(item.get("session_id") or "")),
            "edges": sorted(
                (
                    dict(edge)
                    for edge in state["session_edges"]
                    if edge.get("parent_session_id") in selected_ids
                    and edge.get("child_session_id") in selected_ids
                ),
                key=lambda item: (item["parent_session_id"], item["child_session_id"]),
            ),
            "filter": {
                "active": True,
                "completion_state": "incomplete",
                "updated_within_hours": hours,
                "cutoff": cutoff.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            },
        }

    def session_goal_dag(
        self,
        session_id: str,
        *,
        include_descendants: bool = True,
    ) -> dict[str, Any]:
        """Return the Goal-bearing DAG context relevant to one Session."""
        state = self.load()
        if session_id not in state["sessions"]:
            raise StoreError(f"unknown session: {session_id}")

        edges = state["session_edges"]
        parents_by_child: dict[str, list[str]] = {}
        children_by_parent: dict[str, list[str]] = {}
        for edge in edges:
            parent_id = str(edge.get("parent_session_id") or "")
            child_id = str(edge.get("child_session_id") or "")
            if not parent_id or not child_id:
                continue
            parents_by_child.setdefault(child_id, []).append(parent_id)
            children_by_parent.setdefault(parent_id, []).append(child_id)

        ancestor_distances = self._session_graph_distances(parents_by_child, session_id)
        descendant_distances = (
            self._session_graph_distances(children_by_parent, session_id)
            if include_descendants
            else {session_id: 0}
        )
        selected_ids = set(ancestor_distances) | set(descendant_distances)
        current_goals = {
            str(goal.get("session_id") or ""): goal
            for goal in self._current_goals(state)
        }

        nodes: list[dict[str, Any]] = []
        for selected_id in selected_ids:
            session = state["sessions"].get(selected_id)
            if not session:
                continue
            if selected_id == session_id:
                relation = "focus"
                distance = 0
            elif selected_id in ancestor_distances:
                relation = "ancestor"
                distance = ancestor_distances[selected_id]
            else:
                relation = "descendant"
                distance = descendant_distances[selected_id]
            goal = current_goals.get(selected_id)
            nodes.append(
                {
                    "session_id": selected_id,
                    "title": str(session.get("title") or ""),
                    "active": session.get("active") is True,
                    "unit_id": session.get("unit_id"),
                    "relation_to_focus": relation,
                    "distance": distance,
                    "goal": (
                        {
                            "goal_id": str(goal.get("goal_id") or ""),
                            "body": str(goal.get("body") or ""),
                            "completion_state": str(goal.get("completion_state") or ""),
                            "completion_reason": str(goal.get("completion_reason") or ""),
                        }
                        if goal
                        else None
                    ),
                }
            )

        relation_order = {"ancestor": 0, "focus": 1, "descendant": 2}
        nodes.sort(
            key=lambda item: (
                relation_order[str(item["relation_to_focus"])],
                -int(item["distance"]) if item["relation_to_focus"] == "ancestor" else int(item["distance"]),
                str(item["session_id"]),
            )
        )
        selected_edges: list[dict[str, Any]] = []
        for edge in edges:
            parent_id = str(edge.get("parent_session_id") or "")
            child_id = str(edge.get("child_session_id") or "")
            if parent_id not in selected_ids or child_id not in selected_ids:
                continue
            edge_context = dict(edge)
            edge_context["relationship"] = (
                "subgoal"
                if parent_id in current_goals and child_id in current_goals
                else "context"
            )
            selected_edges.append(edge_context)
        selected_edges.sort(
            key=lambda item: (str(item.get("parent_session_id") or ""), str(item.get("child_session_id") or "")),
        )
        return {
            "focus_session_id": session_id,
            "include_descendants": include_descendants,
            "nodes": nodes,
            "edges": selected_edges,
        }

    @staticmethod
    def _session_graph_distances(adjacency: dict[str, list[str]], start: str) -> dict[str, int]:
        distances = {start: 0}
        pending = [start]
        while pending:
            current = pending.pop(0)
            next_distance = distances[current] + 1
            for adjacent in adjacency.get(current, []):
                if adjacent in distances and distances[adjacent] <= next_distance:
                    continue
                distances[adjacent] = next_distance
                pending.append(adjacent)
        return distances

    def parents(self, session_id: str) -> list[dict[str, Any]]:
        state = self.load()
        if session_id not in state["sessions"]:
            raise StoreError(f"unknown session: {session_id}")
        return sorted(
            [edge for edge in state["session_edges"] if edge.get("child_session_id") == session_id],
            key=lambda item: item["parent_session_id"],
        )

    def children(self, session_id: str) -> list[dict[str, Any]]:
        state = self.load()
        if session_id not in state["sessions"]:
            raise StoreError(f"unknown session: {session_id}")
        return sorted(
            [edge for edge in state["session_edges"] if edge.get("parent_session_id") == session_id],
            key=lambda item: item["child_session_id"],
        )

    def messages(self, session_id: str | None = None) -> list[dict[str, Any]]:
        state = self.load()
        messages = self._all_messages(state)
        if session_id:
            messages = self._session_messages(state, session_id)
        return list(messages)

    def session_log(
        self,
        session_id: str,
        *,
        from_seq: int | None = None,
        to_seq: int | None = None,
        limit: int = 10,
        role: str | None = None,
        after_cursor: bool = False,
    ) -> list[dict[str, Any]]:
        state = self.load()
        session = state["sessions"].get(session_id)
        if not session:
            raise StoreError(f"unknown session: {session_id}")
        if after_cursor:
            if not role:
                raise StoreError("--after-cursor requires --role")
            self._ensure_role_cursors(session)
            cursor = int(session.setdefault("role_cursors", {}).get(role) or 0)
            from_seq = max(int(from_seq or 0), cursor + 1)

        entries = []
        for entry in self._session_log_entries(state, session_id):
            seq = int(entry.get("seq") or 0)
            if from_seq is not None and seq < int(from_seq):
                continue
            if to_seq is not None and seq > int(to_seq):
                continue
            rendered = dict(entry)
            message = self._message_for_log_entry(state, entry)
            if message:
                rendered["message"] = dict(message)
            entries.append(rendered)
        if limit and limit > 0:
            entries = entries[-limit:]
        return entries
