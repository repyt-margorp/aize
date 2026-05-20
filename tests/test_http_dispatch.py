from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from runtime.http_dispatch import send_http_dispatch_plan  # noqa: E402
from runtime.dispatch_queue import dispatch_priority, order_dispatch_messages  # noqa: E402
from runtime.message_builder import dispatch_pending_opens_visible_turn  # noqa: E402


class HttpDispatchTests(unittest.TestCase):
    def test_send_http_dispatch_plan_builds_role_specific_pending_messages(self) -> None:
        messages: list[dict[str, Any]] = []

        worker_queued, error = send_http_dispatch_plan(
            dispatch_plan=[
                {
                    "channel": "interactive",
                    "service_id": "service-codex-001",
                    "session_id": "entrance",
                    "reason": "http_user_dialogue",
                },
                {
                    "channel": "worker",
                    "service_id": "service-codex-002",
                    "session_id": "entrance",
                    "reason": "interactive_worker_request",
                },
                {
                    "channel": "goal_manager",
                    "service_id": "service-codex-003",
                    "session_id": "entrance",
                    "reason": "goal_manager_review",
                },
                {
                    "channel": "forwarded",
                    "service_id": "service-codex-004",
                    "session_id": "dev-session",
                    "reason": "http_prompt",
                },
            ],
            manifest={"node_id": "node-aize", "run_id": "run-main"},
            from_service_id="service-http-001",
            process_id="proc-http",
            run_id="run-main",
            worker_request_id="interactive-worker-123",
            username="repyt",
            auth_context={"username": "repyt"},
            selected_agent_profile={"provider": "codex", "session_slot": "interactive_agent"},
            slot_agent_id=lambda service_id, session_id, slot: f"{service_id}@@{session_id}@@{slot}",
            resolve_goal_manager_agent_id=lambda service_id, session_id: f"{service_id}@@{session_id}@@goal_manager",
            send_router_control=lambda message: messages.append(message) is None or True,
        )

        self.assertTrue(worker_queued)
        self.assertEqual(error, "")
        self.assertEqual([message["to"] for message in messages], [
            "service-codex-003",
            "service-codex-001",
            "service-codex-002",
            "service-codex-004",
        ])
        self.assertEqual([message["payload"]["reason"] for message in messages], [
            "goal_manager_review",
            "http_user_dialogue",
            "interactive_worker_request",
            "http_prompt",
        ])
        self.assertEqual(messages[0]["meta"]["dispatch_priority"], 80)
        self.assertEqual(messages[0]["meta"]["session_agent_id"], "service-codex-003@@entrance@@goal_manager")
        self.assertEqual(messages[1]["meta"]["agent_profile"]["session_slot"], "interactive_agent")
        self.assertEqual(messages[2]["meta"]["run_id"], "interactive-worker-123")
        self.assertEqual(messages[2]["meta"]["session_agent_id"], "service-codex-002@@entrance@@worker_agent")
        self.assertNotIn("session_agent_id", messages[3]["meta"])

    def test_send_http_dispatch_plan_reports_first_router_injection_failure(self) -> None:
        def reject_worker(message: dict[str, Any]) -> bool:
            return str(message.get("to") or "") != "service-codex-worker"

        worker_queued, error = send_http_dispatch_plan(
            dispatch_plan=[
                {
                    "channel": "worker",
                    "service_id": "service-codex-worker",
                    "session_id": "entrance",
                    "reason": "interactive_worker_request",
                }
            ],
            manifest={"node_id": "node-aize", "run_id": "run-main"},
            from_service_id="service-http-001",
            process_id="proc-http",
            run_id="run-main",
            worker_request_id="",
            username="repyt",
            auth_context=None,
            selected_agent_profile=None,
            slot_agent_id=lambda service_id, session_id, slot: f"{service_id}@@{session_id}@@{slot}",
            resolve_goal_manager_agent_id=lambda service_id, session_id: "",
            send_router_control=reject_worker,
        )

        self.assertTrue(worker_queued)
        self.assertEqual(error, "router_control_injection_failed:worker")

    def test_dispatch_queue_orders_messages_by_reason_priority(self) -> None:
        messages = [
            {"payload": {"reason": "turn_completed"}, "meta": {}},
            {"payload": {"reason": "panic_recovery"}, "meta": {}},
            {"payload": {"reason": "child_session_completed"}, "meta": {}},
        ]

        ordered = order_dispatch_messages(messages)

        self.assertEqual([message["payload"]["reason"] for message in ordered], [
            "panic_recovery",
            "child_session_completed",
            "turn_completed",
        ])
        self.assertGreater(dispatch_priority("panic_recovery"), dispatch_priority("child_session_completed"))
        self.assertGreater(dispatch_priority("child_session_panic"), dispatch_priority("child_session_completed"))

    def test_child_session_panic_dispatch_is_system_queue_work(self) -> None:
        message = {
            "type": "dispatch_pending",
            "payload": {"reason": "child_session_panic"},
        }

        self.assertFalse(
            dispatch_pending_opens_visible_turn(
                message,
                '<input index="1" kind="child_session_panic"><role>system</role><text>{}</text></input>',
            )
        )


if __name__ == "__main__":
    unittest.main()
