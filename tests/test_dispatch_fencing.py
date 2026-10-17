from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any


from goal.agents import AgentResult
from goal.defs import GOAL_MANAGER_ROLE, WORKER_AGENT_ROLE
from goal.store import Store
from kernel.defs import ROOT_SESSION_ID, StoreError


class FencedCommitTests(unittest.TestCase):
    def setUp(self) -> None:
        state_root = Path(tempfile.mkdtemp()) / "state"
        self.store = Store(state_root)
        self.store.init()
        self.store.create_session("fence-session", parent_session_ids=[ROOT_SESSION_ID])
        self.store.append_user_input(
            "fence-session",
            sender="root",
            body="do the first requirement",
            reply_to="fence-console",
        )
        self.store.update_goal("fence-session", body="first requirement", created_by="root")
        self.goal_id = self._current_goal(self.store.load())["goal_id"]

    def _acquire_lease(self) -> dict[str, Any]:
        with self.store._state_lock():
            lease = self.store._acquire_dispatch_lease_locked(session_id="fence-session")
        return lease

    def _acquire_role_lease(self, role: str) -> dict[str, Any]:
        lease = self._acquire_lease()
        self.assertIsNotNone(lease)
        self.assertEqual(lease["role"], role)
        return lease

    def _extend_window(self, lease: dict[str, Any]) -> None:
        with self.store._state_lock():
            state = self.store.load()
            run = state["dispatch_runs"][lease["run"]["run_id"]]
            run["observed_to_seq"] = self.store._latest_session_log_seq(state, session_id="fence-session")
            self.store.save(state)

    def _commit(self, lease: dict[str, Any], *, role: str, output: str) -> dict[str, Any]:
        with self.store._state_lock():
            if role == GOAL_MANAGER_ROLE:
                return self.store._commit_goal_manager_run_locked(
                    lease,
                    {"prompt": "p", "agent_result": AgentResult(provider="local", output=output)},
                )
            return self.store._commit_worker_run_locked(
                lease,
                {
                    "prompt": "p",
                    "agent_result": AgentResult(provider="local", output=output),
                    "dispatch_messages": [],
                },
            )

    def _declare(self, *, completion_state: str, reason: str, run_id: str) -> dict[str, Any]:
        return self.store.set_goal_completion_state_from_runtime(
            "fence-session",
            completion_state=completion_state,
            reason=reason,
            actor=GOAL_MANAGER_ROLE,
            run_id=run_id,
        )

    def _interrupt_run(self, run_id: str) -> None:
        with self.store._state_lock():
            state = self.store.load()
            state["dispatch_runs"][run_id]["lease_state"] = "interrupted"
            state["dispatch_runs"][run_id]["current_phase"] = None
            self.store.save(state)

    def _current_goal(self, state: dict[str, Any]) -> dict[str, Any]:
        candidates = [
            goal
            for goal in state["goals"].values()
            if str(goal.get("session_id") or "") == "fence-session" and not goal.get("archived_at")
        ]
        return sorted(
            candidates,
            key=lambda item: (str(item.get("created_at") or ""), str(item.get("goal_id") or "")),
        )[-1]

    def _fence_signals(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            entry
            for entry in self.store.session_log("fence-session", limit=0)
            if entry.get("kind") == "SystemSignal"
            and entry["event"].get("signal_type") == "StaleDispatchResultDiscarded"
        ]

    def test_user_input_between_declaration_and_commit_fences_completion(self) -> None:
        lease = self._acquire_role_lease(GOAL_MANAGER_ROLE)
        run_id = lease["run"]["run_id"]
        self._declare(completion_state="complete", reason="declared done", run_id=run_id)
        self.store.append_user_input("fence-session", sender="root", body="newer requirement")
        result = self._commit(lease, role=GOAL_MANAGER_ROLE, output="stale plan")

        state = self.store.load()
        goal = self._current_goal(state)
        self.assertEqual(goal["completion_state"], "incomplete")
        self.assertEqual(result["run"]["stale_result_fenced"], True)
        self.assertIsNone(result["state_transition"])
        signals = self._fence_signals(state)
        self.assertEqual(len(signals), 1)
        self.assertEqual(
            signals[0]["event"]["data"]["fence_reason"],
            "a newer Session message arrived after the run's observation window",
        )
        run_record = state["dispatch_runs"][run_id]
        self.assertEqual(run_record["stale_results"][0]["output"], "stale plan")
        with self.store._state_lock():
            self.store._refresh_dispatch_readiness(state)
            ready = [
                entry
                for entry in state["dispatch_readiness"]
                if entry.get("goal_id") == self.goal_id
                and entry.get("role") == GOAL_MANAGER_ROLE
                and entry.get("status") == "ready"
            ]
        self.assertTrue(ready)

    def test_goal_body_revision_between_declaration_and_commit_fences_completion(self) -> None:
        lease = self._acquire_role_lease(GOAL_MANAGER_ROLE)
        run_id = lease["run"]["run_id"]
        self._extend_window(lease)
        self._declare(completion_state="complete", reason="declared done", run_id=run_id)
        self.store.update_goal("fence-session", body="replacement requirement", created_by="root")
        result = self._commit(lease, role=GOAL_MANAGER_ROLE, output="stale plan")

        state = self.store.load()
        goal = self._current_goal(state)
        self.assertEqual(goal["body"], "replacement requirement")
        self.assertEqual(goal["completion_state"], "incomplete")
        self.assertEqual(result["run"]["stale_result_fenced"], True)
        body_updates = [
            entry
            for entry in self.store.session_log("fence-session", limit=0)
            if entry.get("kind") == "GoalBodyUpdated"
            and entry["event"]["previous_body"] == "first requirement"
        ]
        self.assertEqual(len(body_updates), 1)
        self.assertEqual(body_updates[0]["event"]["previous_body"], "first requirement")
        with self.store._state_lock():
            self.store._refresh_dispatch_readiness(state)
            ready = [
                entry
                for entry in state["dispatch_readiness"]
                if entry.get("goal_id") == self.goal_id
                and entry.get("role") == GOAL_MANAGER_ROLE
                and entry.get("status") == "ready"
            ]
        self.assertTrue(ready)

    def test_declaration_after_declared_outcome_is_rejected(self) -> None:
        lease = self._acquire_role_lease(GOAL_MANAGER_ROLE)
        run_id = lease["run"]["run_id"]
        self._declare(completion_state="complete", reason="declared done", run_id=run_id)
        with self.assertRaises(StoreError) as raised:
            self._declare(completion_state="incomplete", reason="second declaration", run_id=run_id)
        self.assertIn("already declared", str(raised.exception))

    def test_declaration_rejects_goal_body_revision_window(self) -> None:
        lease = self._acquire_role_lease(GOAL_MANAGER_ROLE)
        run_id = lease["run"]["run_id"]
        self.store.update_goal("fence-session", body="replacement requirement", created_by="root")
        with self.assertRaises(StoreError) as raised:
            self._declare(completion_state="complete", reason="declared done", run_id=run_id)
        self.assertIn("observed SessionGoal changed", str(raised.exception))

    def test_interrupted_goal_manager_run_returning_late_is_fenced(self) -> None:
        lease = self._acquire_role_lease(GOAL_MANAGER_ROLE)
        run_id = lease["run"]["run_id"]
        self._interrupt_run(run_id)
        result = self._commit(lease, role=GOAL_MANAGER_ROLE, output="late decision")

        state = self.store.load()
        goal = self._current_goal(state)
        self.assertEqual(goal["completion_state"], "incomplete")
        self.assertEqual(state["dispatch_runs"][run_id]["lease_state"], "interrupted")
        self.assertEqual(result["run"]["stale_result_fenced"], True)
        self.assertEqual(len(state["dispatch_runs"][run_id]["stale_results"]), 1)
        self.assertEqual(len(self._fence_signals(state)), 1)

    def test_interrupted_worker_run_returning_late_is_fenced(self) -> None:
        lease = self._acquire_role_lease(GOAL_MANAGER_ROLE)
        self._declare(completion_state="incomplete", reason="delegate the work", run_id=lease["run"]["run_id"])
        self._commit(lease, role=GOAL_MANAGER_ROLE, output="worker needed")
        lease = self._acquire_lease()
        self.assertIsNotNone(lease)
        self.assertEqual(lease["role"], WORKER_AGENT_ROLE)
        run_id = lease["run"]["run_id"]
        self._interrupt_run(run_id)
        result = self._commit(lease, role=WORKER_AGENT_ROLE, output="late report")

        state = self.store.load()
        self.assertEqual(state["dispatch_runs"][run_id]["lease_state"], "interrupted")
        self.assertEqual(result["run"]["stale_result_fenced"], True)

    def test_acquired_goal_manager_commit_window_consolidates_completion(self) -> None:
        lease = self._acquire_role_lease(GOAL_MANAGER_ROLE)
        self._extend_window(lease)
        self._declare(completion_state="complete", reason="clean completion", run_id=lease["run"]["run_id"])
        result = self._commit(lease, role=GOAL_MANAGER_ROLE, output="done")

        state = self.store.load()
        goal = self._current_goal(state)
        self.assertEqual(goal["completion_state"], "complete")
        self.assertEqual(result["state_transition"]["completion_state"], "complete")
        self.assertEqual(len(self._fence_signals(state)), 0)


if __name__ == "__main__":
    unittest.main()
