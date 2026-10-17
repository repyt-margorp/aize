from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


from goal.store import Store
from kernel.defs import ROOT_SESSION_ID, ROOT_UNIT_ID, StoreError


class UnitPauseTests(unittest.TestCase):
    def _store(self) -> Store:
        state_root = Path(tempfile.mkdtemp()) / "state"
        store = Store(state_root)
        store.init()
        return store

    def test_pause_blocks_every_activation_path_and_dispatch(self) -> None:
        store = self._store()
        store.create_unit(
            "paused-unit",
            display_name="Paused",
            goal_text="scheduled body",
            schedule={"resolver": "explicit", "next_run_at": "2030-01-01T00:00:00Z", "enabled": True},
        )
        store.create_session("unit-session-a", parent_session_ids=[ROOT_SESSION_ID], unit_id="paused-unit")
        store.append_user_input("unit-session-a", sender="root", body="start")
        store.update_goal("unit-session-a", body="incomplete goal", created_by="root")
        with store._state_lock():
            lease = store._acquire_dispatch_lease_locked(session_id="unit-session-a")
        self.assertIsNotNone(lease)

        store.set_unit_active("paused-unit", active=False)
        unit = store.load()["units"]["paused-unit"]
        self.assertEqual(unit["status"], "paused")

        with self.assertRaises(StoreError):
            store.create_session("second-session", parent_session_ids=[ROOT_SESSION_ID], unit_id="paused-unit")
        with self.assertRaises(StoreError):
            store.start_goal_session(
                "third-session",
                unit_id="paused-unit",
                parent_session_ids=[ROOT_SESSION_ID],
                label="third",
                body="third",
                created_by="root",
            )
        self.assertEqual(store.run_scheduled_units(), [])
        self.assertEqual(store.run_startup_units(), [])
        with store._state_lock():
            self.assertIsNone(store._acquire_dispatch_lease_locked(session_id="unit-session-a"))
        liveness = store.dispatch_liveness()
        self.assertEqual(liveness["gap_count"], 0)

    def test_activate_resumes_dispatch(self) -> None:
        store = self._store()
        store.create_unit(
            "resume-unit",
            display_name="Resume",
            goal_text="scheduled body",
            schedule={"resolver": "explicit", "next_run_at": "2030-01-01T00:00:00Z", "enabled": True},
        )
        store.create_session("resume-session", parent_session_ids=[ROOT_SESSION_ID], unit_id="resume-unit")
        store.append_user_input("resume-session", sender="root", body="start")
        store.update_goal("resume-session", body="incomplete goal", created_by="root")
        store.set_unit_active("resume-unit", active=False)
        with store._state_lock():
            with self.assertRaises(StoreError):
                store._acquire_dispatch_lease_locked(session_id="resume-session")
        store.set_unit_active("resume-unit", active=True)
        with store._state_lock():
            lease = store._acquire_dispatch_lease_locked(session_id="resume-session")
        self.assertIsNotNone(lease)

    def test_root_unit_cannot_be_paused(self) -> None:
        store = self._store()
        with self.assertRaises(StoreError):
            store.set_unit_active(ROOT_UNIT_ID, active=False)
        self.assertEqual(store.load()["units"][ROOT_UNIT_ID]["status"], "active")

    def test_unknown_unit_is_rejected(self) -> None:
        store = self._store()
        with self.assertRaises(StoreError):
            store.set_unit_active("missing-unit", active=False)


if __name__ == "__main__":
    unittest.main()
