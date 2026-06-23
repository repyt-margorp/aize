from __future__ import annotations

from typing import Any

from store_defs import StoreError


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
        messages = state["messages"]
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
        queued_dispatch_entries = [
            entry for entry in state.get("dispatch_queue", []) if entry.get("status") == "queued"
        ]
        acquired_dispatch_entries = [
            entry for entry in state.get("dispatch_queue", []) if entry.get("status") == "acquired"
        ]
        return {
            "version": state["version"],
            "created_at": state["created_at"],
            "unit_count": len(state["units"]),
            "session_count": len(state["sessions"]),
            "active_session_count": len(active_sessions),
            "inactive_session_count": len(inactive_sessions),
            "session_edge_count": len(state["session_edges"]),
            "account_count": len(state["accounts"]),
            "goal_count": len(current_goals),
            "incomplete_goal_count": len(incomplete_goals),
            "complete_goal_count": len(complete_goals),
            "active_incomplete_goal_count": len(active_incomplete_goals),
            "dispatch_run_count": len(state["dispatch_runs"]),
            "acquired_dispatch_lease_count": len(acquired_dispatch_leases),
            "dispatch_queue_count": len(state.get("dispatch_queue", [])),
            "queued_dispatch_count": len(queued_dispatch_entries),
            "acquired_dispatch_count": len(acquired_dispatch_entries),
            "dispatch_lot_size": max(1, int(state.setdefault("runtime_settings", {}).get("dispatch_lot_size") or 1)),
            "agent_profile_count": len(state["agent_profiles"]),
            "agent_thread_count": len(state["agent_threads"]),
            "message_count": len(messages),
            "endpoint_cursor_count": len(state.get("endpoint_cursors", {})),
        }

    def goals(self, session_id: str | None = None) -> list[dict[str, Any]]:
        state = self.load()
        goals = self._current_goals(state)
        if session_id:
            goals = [goal for goal in goals if goal.get("session_id") == session_id]
        return sorted(goals, key=lambda item: (item["created_at"], item["goal_id"]))

    def dispatch_runs(self, session_id: str | None = None) -> list[dict[str, Any]]:
        state = self.load()
        runs = list(state["dispatch_runs"].values())
        if session_id:
            runs = [run for run in runs if run.get("session_id") == session_id]
        return sorted(runs, key=lambda item: (item["created_at"], item["run_id"]))

    def dispatch_queue(self, session_id: str | None = None) -> list[dict[str, Any]]:
        state = self.load()
        indexed_entries = list(enumerate(state.get("dispatch_queue", [])))
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
                -int(item[1].get("priority") or 0),
                str(item[1].get("queued_at") or ""),
                item[0],
            ),
        )
        return [entry for _, entry in sorted_entries]

    def agent_threads(self, session_id: str | None = None) -> list[dict[str, Any]]:
        state = self.load()
        threads = list(state["agent_threads"].values())
        if session_id:
            threads = [thread for thread in threads if thread.get("session_id") == session_id]
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
        messages = state["messages"]
        if session_id:
            indexed_ids = {
                str(item.get("message_id") or "")
                for item in state.setdefault("message_index", [])
                if item.get("session_id") == session_id
            }
            messages = [
                msg
                for msg in messages
                if str(msg.get("message_id") or "") in indexed_ids
            ]
        return list(messages)
