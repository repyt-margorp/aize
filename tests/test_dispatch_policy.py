from __future__ import annotations

import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime
from io import StringIO

from cli_render import print_dispatch_readiness
from dispatch_policy import select_role_dispatch_readiness
from dispatch_projection import DispatchLogItem, derive_role_dispatch_readiness


class DispatchProjectionTests(unittest.TestCase):
    def test_user_input_makes_only_goal_manager_ready(self) -> None:
        item = DispatchLogItem(
            entry={"seq": 4, "kind": "Message", "log_id": "log-4"},
            message={
                "message_id": "msg-4",
                "from": "account:root",
                "to": "session:work",
                "payload": {"user_input": True, "body": "continue"},
            },
        )

        readiness = derive_role_dispatch_readiness(
            [item], role="GoalManager", session_id="work", active_worker=False
        )

        self.assertIsNotNone(readiness)
        assert readiness is not None
        self.assertEqual(readiness.from_log_seq, 4)
        self.assertEqual(readiness.observed_to_seq, 4)
        self.assertEqual(readiness.wake_reasons[0].kind, "UserInput")
        self.assertNotIn("priority", readiness.wake_reasons[0].to_dict())
        self.assertIsNone(
            derive_role_dispatch_readiness(
                [item], role="WorkerAgent", session_id="work", active_worker=False
            )
        )

    def test_worker_requires_explicit_worker_request(self) -> None:
        ordinary_message = DispatchLogItem(
            entry={"seq": 5, "kind": "Message", "log_id": "log-5"},
            message={
                "message_id": "msg-5",
                "from": "agent:GoalManager",
                "to": "session:work",
                "payload": {"body": "observe only"},
            },
        )
        worker_request = DispatchLogItem(
            entry={"seq": 6, "kind": "Message", "log_id": "log-6"},
            message={
                "message_id": "msg-6",
                "from": "agent:GoalManager",
                "to": "session:work",
                "payload": {"worker_request": True, "body": "implement"},
            },
        )

        self.assertIsNone(
            derive_role_dispatch_readiness(
                [ordinary_message], role="WorkerAgent", session_id="work", active_worker=False
            )
        )
        readiness = derive_role_dispatch_readiness(
            [ordinary_message, worker_request],
            role="WorkerAgent",
            session_id="work",
            active_worker=False,
        )
        self.assertIsNotNone(readiness)
        assert readiness is not None
        self.assertEqual(readiness.from_log_seq, 5)
        self.assertEqual(readiness.observed_to_seq, 6)
        self.assertEqual([reason.kind for reason in readiness.wake_reasons], ["WorkerRequest"])

    def test_multiple_wake_reasons_share_one_log_window(self) -> None:
        items = [
            DispatchLogItem(
                entry={"seq": 10, "kind": "Message", "log_id": "log-10"},
                message={
                    "message_id": "msg-user",
                    "from": "account:root",
                    "to": "session:work",
                    "payload": {"user_input": True},
                },
            ),
            DispatchLogItem(
                entry={"seq": 11, "kind": "Message", "log_id": "log-11"},
                message={
                    "message_id": "msg-worker",
                    "from": "agent:WorkerAgent",
                    "to": "session:work",
                    "payload": {"body": "done"},
                },
            ),
        ]

        readiness = derive_role_dispatch_readiness(
            items, role="GoalManager", session_id="work", active_worker=False
        )

        self.assertIsNotNone(readiness)
        assert readiness is not None
        self.assertEqual((readiness.from_log_seq, readiness.observed_to_seq), (10, 11))
        self.assertEqual([reason.kind for reason in readiness.wake_reasons], ["UserInput", "WorkerReport"])


class DispatchSelectionTests(unittest.TestCase):
    def test_session_policy_and_waiting_age_determine_order(self) -> None:
        readiness = [
            {
                "readiness_id": "older",
                "session_id": "one",
                "goal_id": "goal-one",
                "role": "GoalManager",
                "status": "ready",
                "first_ready_at": "2026-08-08T00:00:00Z",
            },
            {
                "readiness_id": "higher-session-priority",
                "session_id": "two",
                "goal_id": "goal-two",
                "role": "WorkerAgent",
                "status": "ready",
                "first_ready_at": "2026-08-08T00:01:00Z",
            },
        ]
        goals = {
            "goal-one": {"completion_state": "incomplete"},
            "goal-two": {"completion_state": "incomplete"},
        }
        sessions = {
            "one": {"active": True, "scheduling_policy": {"class": "normal", "base_priority": 0}},
            "two": {"active": True, "scheduling_policy": {"class": "normal", "base_priority": 10}},
        }

        decision = select_role_dispatch_readiness(
            readiness,
            goals=goals,
            sessions=sessions,
            acquired_roles=set(),
            now=datetime(2026, 8, 8, 0, 2, tzinfo=UTC),
        )

        self.assertEqual(decision.readiness_index, 1)
        self.assertEqual(decision.scheduling_score, 11)
        self.assertIn("session=10", decision.scheduling_reason)

    def test_skips_a_role_with_an_acquired_run(self) -> None:
        readiness = [
            {
                "readiness_id": "busy",
                "session_id": "work",
                "goal_id": "goal-work",
                "role": "WorkerAgent",
                "status": "ready",
                "first_ready_at": "2026-08-08T00:00:00Z",
            }
        ]

        decision = select_role_dispatch_readiness(
            readiness,
            goals={"goal-work": {"completion_state": "incomplete"}},
            sessions={"work": {"active": True}},
            acquired_roles={("work", "WorkerAgent")},
            now=datetime(2026, 8, 8, tzinfo=UTC),
        )

        self.assertIsNone(decision.readiness_index)


class DispatchReadinessRenderingTests(unittest.TestCase):
    def test_cli_render_shows_window_and_wake_reasons_without_priority(self) -> None:
        output = StringIO()

        with redirect_stdout(output):
            print_dispatch_readiness(
                [
                    {
                        "readiness_id": "ready-one",
                        "session_id": "work",
                        "goal_id": "goal-work",
                        "role": "GoalManager",
                        "status": "ready",
                        "from_log_seq": 10,
                        "observed_to_seq": 12,
                        "first_ready_at": "2026-08-08T00:00:00Z",
                        "wake_reasons": [{"seq": 12, "kind": "WorkerReport"}],
                    }
                ]
            )

        rendered = output.getvalue()
        self.assertIn("range=10..12", rendered)
        self.assertIn("wake_reasons: WorkerReport", rendered)
        self.assertNotIn("priority", rendered)


if __name__ == "__main__":
    unittest.main()
