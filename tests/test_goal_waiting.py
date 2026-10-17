from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any


from goal.agents import AgentResult
from goal.defs import GOAL_MANAGER_ROLE, WORKER_AGENT_ROLE
from goal.store import Store
from kernel.defs import ROOT_SESSION_ID, StoreError


class GoalWaitingTests(unittest.TestCase):
    def _session(self) -> tuple[Store, str]:
        state_root = Path(tempfile.mkdtemp()) / "state"
        store = Store(state_root)
        store.init()
        store.create_session("wait-session", parent_session_ids=[ROOT_SESSION_ID])
        store.append_user_input("wait-session", sender="root", body="start work", reply_to="console")
        store.update_goal("wait-session", body="waited goal", created_by="root")
        lease = self._acquire(store)
        self.assertEqual(lease["role"], GOAL_MANAGER_ROLE)
        return store, lease["run"]["run_id"]

    @staticmethod
    def _acquire(store: Store, session_id: str = "wait-session"):
        with store._state_lock():
            return store._acquire_dispatch_lease_locked(session_id=session_id)

    def _commit_declared(self, store: Store, run_id: str) -> dict[str, Any]:
        with store._state_lock():
            return store._commit_goal_manager_run_locked(
                self._lease_by_run(store, run_id),
                {"prompt": "p", "agent_result": AgentResult(provider="local", output="waiting")},
            )

    def _lease_by_run(self, store: Store, run_id: str) -> dict[str, Any]:
        state = store.load()
        run = state["dispatch_runs"][run_id]
        goal = next(
            goal
            for goal in state["goals"].values()
            if str(goal.get("session_id") or "") == run["session_id"]
        )
        return {
            "goal": dict(goal),
            "session": dict(state["sessions"][run["session_id"]]),
            "unit": None,
            "run": dict(run),
            "readiness_id": run["readiness_id"],
            "session_messages": [],
            "role": run["role"],
            "resume_token": "",
            "recovery_context": "",
        }

    def test_waiting_declaration_suppresses_worker_and_liveness_wake(self) -> None:
        store, run_id = self._session()
        deadline = self._future_deadline(seconds=60)
        store.set_goal_waiting_from_runtime(
            "wait-session",
            wait={"kind": "deadline", "wait_until": deadline},
            actor=GOAL_MANAGER_ROLE,
            run_id=run_id,
        )
        result = self._commit_declared(store, run_id)

        self.assertEqual(result["run"]["goal_manager_status"], "waiting")
        self.assertEqual(result["run"]["lease_state"], "released")
        state = store.load()
        goal = self._goal(store)
        self.assertEqual(goal["completion_state"], "incomplete")
        self.assertEqual(goal["waiting"]["kind"], "deadline")
        waiting_transitions = [
            entry
            for entry in store.session_log("wait-session", limit=0)
            if entry.get("kind") == "GoalStateChanged"
            and entry["event"].get("completion_state") == "waiting"
        ]
        self.assertEqual(len(waiting_transitions), 1)
        with store._state_lock():
            store._refresh_dispatch_readiness(state)
            store._reconcile_dispatch_liveness(state)
            store.save(state)
            ready_roles = {
                (entry.get("goal_id"), entry.get("role"))
                for entry in state["dispatch_readiness"]
                if entry.get("status") == "ready"
            }
        self.assertEqual(ready_roles, set())
        liveness_report = store.dispatch_liveness()
        self.assertEqual(liveness_report["gap_count"], 0)
        self.assertEqual(liveness_report["waiting_count"], 1)

    def test_deadline_wake_clears_waiting_and_wakes_goal_manager(self) -> None:
        store, run_id = self._session()
        store.set_goal_waiting_from_runtime(
            "wait-session",
            wait={"kind": "deadline", "wait_until": self._future_deadline(seconds=61)},
            actor=GOAL_MANAGER_ROLE,
            run_id=run_id,
        )
        self._commit_declared(store, run_id)
        state = store.load()
        goal = self._goal(store)
        state["goals"][goal["goal_id"]]["waiting"]["wait_until"] = self._now_deadline()
        store.save(state)

        with store._state_lock():
            self.assertTrue(store._refresh_dispatch_readiness(state))
            store._reconcile_dispatch_liveness(state)
            store.save(state)

        state = store.load()
        self.assertIsNone(self._goal(store).get("waiting"))
        wake_signals = [
            entry
            for entry in store.session_log("wait-session", limit=0)
            if entry.get("kind") == "SystemSignal"
            and entry["event"].get("signal_type") == "GoalWaitingWake"
        ]
        self.assertEqual(len(wake_signals), 1)
        ready_roles = [
            entry.get("role")
            for entry in store.load()["dispatch_readiness"]
            if entry.get("goal_id") == self._goal(store)["goal_id"]
            and entry.get("status") == "ready"
        ]
        self.assertIn(GOAL_MANAGER_ROLE, ready_roles)
        lease = self._acquire(store)
        self.assertIsNotNone(lease)
        self.assertEqual(lease["role"], GOAL_MANAGER_ROLE)

    def test_event_wake_fires_on_matching_wake_event_message(self) -> None:
        store, run_id = self._session()
        store.set_goal_waiting_from_runtime(
            "wait-session",
            wait={"kind": "event", "event_name": "build-ok"},
            actor=GOAL_MANAGER_ROLE,
            run_id=run_id,
        )
        self._commit_declared(store, run_id)
        store.load()
        store.append_runtime_message(
            "wait-session",
            sender=WORKER_AGENT_ROLE,
            recipient="Session",
            body="build finished",
            wake_event="build-ok",
        )
        state = store.load()
        with store._state_lock():
            self.assertTrue(store._refresh_dispatch_readiness(state))
            store._reconcile_dispatch_liveness(state)
            store.save(state)
        state = store.load()
        self.assertIsNone(self._goal(store).get("waiting"))
        wake_signals = [
            entry
            for entry in store.session_log("wait-session", limit=0)
            if entry.get("kind") == "SystemSignal"
            and entry["event"].get("signal_type") == "GoalWaitingWake"
        ]
        self.assertEqual(len(wake_signals), 1)
        ready_roles = [
            entry.get("role")
            for entry in state["dispatch_readiness"]
            if entry.get("goal_id") == self._goal(store)["goal_id"]
            and entry.get("status") == "ready"
        ]
        self.assertIn(GOAL_MANAGER_ROLE, ready_roles)

    def test_mismatched_event_does_not_wake(self) -> None:
        store, run_id = self._session()
        store.set_goal_waiting_from_runtime(
            "wait-session",
            wait={"kind": "event", "event_name": "build-ok"},
            actor=GOAL_MANAGER_ROLE,
            run_id=run_id,
        )
        self._commit_declared(store, run_id)
        store.append_runtime_message(
            "wait-session",
            sender=WORKER_AGENT_ROLE,
            recipient="Session",
            body="build still running",
            wake_event="other-event",
        )
        state = store.load()
        with store._state_lock():
            store._refresh_dispatch_readiness(state)
            store._reconcile_dispatch_liveness(state)
            store.save(state)
        state = store.load()
        goal = self._goal(store)
        self.assertIsNotNone(goal.get("waiting"))
        self.assertEqual(store.dispatch_liveness()["waiting_count"], 1)
        self.assertFalse(
            any(
                entry.get("goal_id") == goal["goal_id"]
                and entry.get("role") == GOAL_MANAGER_ROLE
                and entry.get("status") in {"ready", "acquired"}
                for entry in state["dispatch_readiness"]
            )
        )

    def test_child_wake_fires_on_child_completion(self) -> None:
        store = self._parent_child_session()
        lease = self._acquire(store, session_id="wait-parent")
        self.assertEqual(lease["role"], GOAL_MANAGER_ROLE)
        child_session_id = "wait-child"
        run_id = lease["run"]["run_id"]
        store.set_goal_waiting_from_runtime(
            "wait-parent",
            wait={"kind": "child", "child_session_id": child_session_id},
            actor=GOAL_MANAGER_ROLE,
            run_id=run_id,
        )
        result = self._commit_declared(store, run_id)
        self.assertEqual(result["run"]["goal_manager_status"], "waiting")

        child_goal_id = self._child_goal(store, child_session_id)["goal_id"]
        child_goal = dict(self._child_goal(store, child_session_id))
        with store._state_lock():
            state = store.load()
            child_goal = state["goals"][child_goal_id]
            store._log_child_goal_completed_for_parents(
                state,
                child_goal,
                {
                    "completion_state": "complete",
                    "reason": "child finished",
                    "actor": GOAL_MANAGER_ROLE,
                    "run_id": "run-child-1",
                    "created_at": "2030-02-01T00:00:00Z",
                },
            )
            store.save(state)

        state = store.load()
        with store._state_lock():
            self.assertTrue(store._refresh_dispatch_readiness(state))
            store._reconcile_dispatch_liveness(state)
            store.save(state)
        state = store.load()
        parent_goal = self._parent_goal(store)
        self.assertIsNone(parent_goal.get("waiting"))
        ready_roles = [
            entry.get("role")
            for entry in state["dispatch_readiness"]
            if entry.get("goal_id") == parent_goal["goal_id"]
            and entry.get("status") == "ready"
        ]
        self.assertIn(GOAL_MANAGER_ROLE, ready_roles)

    def test_user_input_cancels_waiting(self) -> None:
        store, run_id = self._session()
        store.set_goal_waiting_from_runtime(
            "wait-session",
            wait={"kind": "deadline", "wait_until": self._future_deadline(seconds=61)},
            actor=GOAL_MANAGER_ROLE,
            run_id=run_id,
        )
        self._commit_declared(store, run_id)
        store.append_user_input("wait-session", sender="root", body="cancel the wait")

        state = store.load()
        with store._state_lock():
            self.assertTrue(store._refresh_dispatch_readiness(state))
            store._reconcile_dispatch_liveness(state)
            store.save(state)
        state = store.load()
        self.assertIsNone(self._goal(store).get("waiting"))
        cancelled = [
            entry
            for entry in store.session_log("wait-session", limit=0)
            if entry.get("kind") == "SystemSignal"
            and entry["event"].get("signal_type") == "GoalWaitingCancelled"
        ]
        self.assertEqual(len(cancelled), 1)
        readiness = [
            entry
            for entry in state["dispatch_readiness"]
            if entry.get("goal_id") == self._goal(store)["goal_id"]
            and entry.get("role") == GOAL_MANAGER_ROLE
        ]
        self.assertTrue(any(entry.get("status") == "ready" for entry in readiness))

    def test_waiting_declaration_guards(self) -> None:
        store, run_id = self._session()
        with self.assertRaises(StoreError):
            store.set_goal_waiting_from_runtime(
                "wait-session",
                wait={"kind": "deadline", "wait_until": self._future_deadline(seconds=61)},
                actor=WORKER_AGENT_ROLE,
                run_id=run_id,
            )
        with self.assertRaises(StoreError):
            store.set_goal_waiting_from_runtime(
                "wait-session",
                wait={"kind": "deadline", "wait_until": self._now_deadline()},
                actor=GOAL_MANAGER_ROLE,
                run_id=run_id,
            )
        with self.assertRaises(StoreError):
            store.set_goal_waiting_from_runtime(
                "wait-session",
                wait={"kind": "event", "event_name": ""},
                actor=GOAL_MANAGER_ROLE,
                run_id=run_id,
            )
        store.set_goal_waiting_from_runtime(
            "wait-session",
            wait={"kind": "deadline", "wait_until": self._future_deadline(seconds=61)},
            actor=GOAL_MANAGER_ROLE,
            run_id=run_id,
        )
        with self.assertRaises(StoreError) as raised:
            store.set_goal_waiting_from_runtime(
                "wait-session",
                wait={"kind": "event", "event_name": "second"},
                actor=GOAL_MANAGER_ROLE,
                run_id=run_id,
            )
        self.assertIn("already declared", str(raised.exception))

    def _parent_child_session(self) -> Store:
        state_root = Path(tempfile.mkdtemp()) / "state"
        store = Store(state_root)
        store.init()
        store.create_session("wait-parent", parent_session_ids=[ROOT_SESSION_ID])
        store.append_user_input("wait-parent", sender="root", body="start parent work", reply_to="console")
        store.update_goal("wait-parent", body="parent goal", created_by="root")
        store.create_session("wait-child", parent_session_ids=["wait-parent"])
        store.append_user_input("wait-child", sender="root", body="child work", reply_to="console-c")
        store.update_goal("wait-child", body="child goal", created_by="root")
        state = store.load()
        child_goal = self._goal_for_session(store, "wait-child")
        with store._state_lock():
            state = store.load()
            store._set_goal_completion_state(
                state,
                state["goals"][child_goal["goal_id"]],
                "complete",
                reason="child declared complete",
                actor=GOAL_MANAGER_ROLE,
            )
            store.save(state)
        return store

    def _child_goal(self, store: Store, session_id: str) -> dict[str, Any]:
        return self._goal_for_session(store, session_id)

    def _goal_for_session(self, store: Store, session_id: str) -> dict[str, Any]:
        state = store.load()
        candidates = [
            goal
            for goal in state["goals"].values()
            if str(goal.get("session_id") or "") == session_id and not goal.get("archived_at")
        ]
        return sorted(
            candidates,
            key=lambda item: (str(item.get("created_at") or ""), str(item.get("goal_id") or "")),
        )[-1]

    def _goal(self, store: Store) -> dict[str, Any]:
        return self._goal_for_session(store, "wait-session")

    def _parent_goal(self, store: Store) -> dict[str, Any]:
        return self._goal_for_session(store, "wait-parent")

    @staticmethod
    def _future_deadline(seconds: int) -> str:
        from datetime import UTC, datetime, timedelta

        when = datetime.now(UTC) + timedelta(seconds=seconds)
        return when.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _now_deadline() -> str:
        from datetime import UTC, datetime

        return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    unittest.main()
