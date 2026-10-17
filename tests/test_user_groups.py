from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


from goal.store import Store
from kernel.defs import ROOT_SESSION_ID, StoreError


class UnitSpawnGroupTests(unittest.TestCase):
    def _member_store(self) -> Store:
        state_root = Path(tempfile.mkdtemp()) / "state"
        store = Store(state_root)
        store.init()
        store.create_user("lead", password="lead-password", groups=["managers"])
        return store

    def test_spawn_groups_gate_every_activation_path(self) -> None:
        store = self._member_store()
        store.create_unit(
            "gated-unit",
            display_name="Gated",
            goal_text="restricted body",
            schedule={"resolver": "explicit", "next_run_at": "2020-01-01T00:00:00Z", "enabled": True},
            spawn_groups=["managers"],
            owner_account="lead",
        )
        with self.assertRaises(StoreError) as raised:
            store.create_session("gate-a", parent_session_ids=[ROOT_SESSION_ID], unit_id="gated-unit")
        self.assertIn("not a member of the required spawn groups", str(raised.exception))
        with self.assertRaises(StoreError):
            store.start_goal_session(
                "gate-b",
                unit_id="gated-unit",
                parent_session_ids=[ROOT_SESSION_ID],
                label="gate b",
                body="gate b",
                created_by="root",
            )

        store.create_session("gate-lead-a", parent_session_ids=[ROOT_SESSION_ID], unit_id="gated-unit", created_by="lead")
        store.start_goal_session(
            "gate-lead-b",
            unit_id="gated-unit",
            parent_session_ids=[ROOT_SESSION_ID],
            label="gate lead",
            body="gate lead",
            created_by="lead",
        )
        started = store.run_scheduled_units()
        started_with_unit_owner = sum(1 for entry in started if entry.get("session"))
        self.assertEqual(started_with_unit_owner, 1)

    def test_scheduler_skips_gated_unit_with_non_member_owner(self) -> None:
        store = self._member_store()
        store.create_unit(
            "blocked-unit",
            display_name="Blocked",
            goal_text="restricted body",
            schedule={"resolver": "explicit", "next_run_at": "2020-01-01T00:00:00Z", "enabled": True},
            spawn_groups=["managers"],
        )
        started = store.run_scheduled_units()
        self.assertEqual([entry for entry in started if entry.get("session")], [])
        skips = [entry["skipped"] for entry in started if entry.get("skipped")]
        self.assertEqual(len(skips), 1)
        self.assertEqual(skips[0]["unit_id"], "blocked-unit")
        self.assertIn("not a member of the required spawn groups", skips[0]["reason"])

        store.set_unit_spawn_groups("blocked-unit", groups=[])
        started_after = store.run_scheduled_units()
        self.assertEqual(sum(1 for entry in started_after if entry.get("session")), 1)

    def test_set_unit_spawn_groups_and_groups_listing(self) -> None:
        store = self._member_store()
        unit = store.create_unit("open-unit", display_name="Open")
        self.assertIsNone(unit.to_dict().get("spawn_groups"))
        restricted = store.set_unit_spawn_groups("open-unit", groups=["managers", "staff"])
        self.assertEqual(restricted["spawn_groups"], ["managers", "staff"])
        with self.assertRaises(StoreError):
            store.create_session("open-gate", parent_session_ids=[ROOT_SESSION_ID], unit_id="open-unit", created_by="root")
        cleared = store.set_unit_spawn_groups("open-unit", groups=[])
        self.assertEqual(cleared["spawn_groups"], [])
        store.create_session("open-gate", parent_session_ids=[ROOT_SESSION_ID], unit_id="open-unit", created_by="root")

        memberships = {
            entry["group"]: entry["members"]
            for entry in store.group_members()
        }
        self.assertIn("managers", memberships)
        self.assertEqual(memberships["managers"], ["lead"])
        self.assertEqual(memberships["admin"], ["root"])
        self.assertNotIn("root", memberships["managers"])

    def test_unknown_spawn_user_is_rejected(self) -> None:
        store = self._member_store()
        store.create_unit("plain-unit", display_name="Plain")
        with self.assertRaises(StoreError) as raised:
            store.create_session("plain-a", parent_session_ids=[ROOT_SESSION_ID], unit_id="plain-unit", created_by="ghost")
        self.assertIn("unknown user", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
