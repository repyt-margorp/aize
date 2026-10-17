from __future__ import annotations

import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch


from goal.defs import GOAL_MANAGER_ROLE
from goal.store import Store
from kernel.defs import ROOT_SESSION_ID


class TransactionGuardTests(unittest.TestCase):
    def _guarded_store(self):
        state_root = Path(tempfile.mkdtemp()) / "state"
        store = Store(state_root)
        store.init()

        local = threading.local()
        store_type = type(store)
        original_lock = store_type._state_lock
        original_save = store_type.save

        @contextmanager
        def watched_lock(inner_self):
            previous = getattr(local, "active", False)
            local.active = True
            try:
                with original_lock(inner_self):
                    yield
            finally:
                local.active = previous

        def watched_save(inner_self, state):
            self.assertTrue(getattr(local, "active", False), "save ran outside the state transaction")
            return original_save(inner_self, state)

        patchers = [
            patch.object(store_type, "_state_lock", watched_lock),
            patch.object(store_type, "save", watched_save),
        ]
        return store, patchers

    def test_every_mutation_save_holds_the_state_transaction(self) -> None:
        store, patchers = self._guarded_store()
        with patchers[0], patchers[1]:
            store.create_user("alice", password="alice-password")
            store.create_unit(
                "unit-a",
                display_name="Unit A",
                goal_text="inspect",
                schedule={"resolver": "explicit", "next_run_at": "2030-01-01T00:00:00Z", "enabled": True},
            )
            store.create_session("session-a", parent_session_ids=[ROOT_SESSION_ID], unit_id="unit-a")
            store.update_goal("session-a", body="first body", created_by="root")
            store.set_session_active("session-a", active=False)
            store.set_session_active("session-a", active=True)
            store.link_sessions(ROOT_SESSION_ID, "session-a")
            store.set_agent_provider(GOAL_MANAGER_ROLE, provider="codex")
            store.set_dispatch_lot_size(2)
            store.configure_unit_schedule(
                "unit-a",
                resolver="explicit",
                fixed_parameters={"interval_seconds": 60},
                next_run_at="2030-01-02T00:00:00Z",
            )
            store.run_scheduled_units()
            store.run_startup_units()
            store.append_user_input("session-a", sender="root", body="hello")
            store.append_message("root", sender="root", recipient="Session", body="note")
            store.receive_message("root")
            result = store.schedule_next_unit_run_from_session(
                "session-a",
                call_parameters={"next_run_at": "2030-01-03T00:00:00Z"},
                actor=GOAL_MANAGER_ROLE,
            )
            self.assertEqual(result["resolution"]["next_run_at"], "2030-01-03T00:00:00Z")

    def test_concurrent_session_creation_and_active_flag_never_lose_records(self) -> None:
        errors: list[BaseException] = []

        def run_worker(store: Store, scenario) -> None:
            try:
                scenario(store)
            except BaseException as exc:
                errors.append(exc)

        for _ in range(3):
            state_root = Path(tempfile.mkdtemp()) / "state"
            store = Store(state_root)
            store.init()
            store.create_session("session-race", parent_session_ids=[ROOT_SESSION_ID])
            store.update_goal("session-race", body="seeding", created_by="root")

            active_writes = []

            def activator(store: Store) -> None:
                expectation = False
                for _ in range(4):
                    store.set_session_active("session-race", active=expectation)
                    active_writes.append(expectation)
                    expectation = not expectation

            def session_creator(store: Store) -> None:
                for index in range(6):
                    store.create_session(f"session-created-{index}", parent_session_ids=[ROOT_SESSION_ID])

            threads = [
                threading.Thread(target=run_worker, args=(store, activator)),
                threading.Thread(target=run_worker, args=(store, session_creator)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])

            state = store.load()
            for index in range(6):
                self.assertIn(f"session-created-{index}", state["sessions"])
            goal = state["goals"][self._current_goal_id(state, "session-race")]
            self.assertEqual(goal["body"], "seeding")
            self.assertEqual(state["sessions"]["session-race"]["active"], active_writes[-1])

    @staticmethod
    def _current_goal_id(state, session_id: str) -> str:
        candidates = [
            (str(goal.get("created_at") or ""), str(goal.get("goal_id") or ""))
            for goal in state["goals"].values()
            if str(goal.get("session_id") or "") == session_id
        ]
        return sorted(candidates)[-1][1]


if __name__ == "__main__":
    unittest.main()
