from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from runtime.persistent_state_pkg.conversation import create_conversation_session, list_sessions
from runtime.session_view import (
    build_session_runtime_summary,
    session_agent_assignment_counts,
    session_registration_metadata,
)


class SessionListingTests(unittest.TestCase):
    def test_list_sessions_does_not_rewrite_session_storage(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / ".aize-runtime"
            runtime_root.mkdir(parents=True)
            create_conversation_session(runtime_root, username="root", label="Root")

            with patch(
                "runtime.persistent_state_pkg.conversation.ensure_session_storage_unlocked"
            ) as ensure_storage:
                sessions = list_sessions(runtime_root, username="root")

            self.assertGreaterEqual(len(sessions), 1)
            ensure_storage.assert_not_called()

    def test_session_summary_exposes_registration_and_unit_metadata(self) -> None:
        session = {
            "session_id": "session-1",
            "label": "Resident",
            "created_at": "2026-05-18T10:00:00Z",
            "updated_at": "2026-05-18T11:00:00Z",
            "goal_updated_at": "2026-05-18T10:30:00Z",
            "session_group": "root",
            "launcher_unit_id": "entrance.service",
            "launcher_template_id": "entrance.service",
            "launcher_display_name": "Entrance",
        }

        summary = build_session_runtime_summary(
            session,
            history_entries=[],
            codex_service_pool=[],
            claude_service_pool=[],
            gemini_service_pool=[],
            default_provider="codex",
        )

        self.assertEqual(summary["registered_at"], "2026-05-18T10:00:00Z")
        self.assertEqual(summary["goal_updated_at"], "2026-05-18T10:30:00Z")
        self.assertTrue(summary["resident_unit_session"])
        self.assertTrue(summary["has_associated_unit_file"])
        self.assertEqual(summary["associated_unit_id"], "entrance.service")
        self.assertEqual(summary["associated_unit_display_name"], "Entrance")

    def test_interface_service_launches_are_marked_resident_without_registered_last_session(self) -> None:
        session = {
            "session_id": "session-entrance-child",
            "label": "Entrance Child",
            "created_at": "2026-05-21T10:00:00Z",
            "updated_at": "2026-05-21T10:05:00Z",
            "session_group": "user",
            "session_ui_mode": "communication",
            "launcher_unit_id": "entrance.service",
            "launcher_template_id": "entrance.service",
            "launcher_display_name": "Entrance",
            "launcher_unit_kind": "interface",
            "launcher_unit_class": "service",
            "launcher_instance_policy": "multi",
        }

        summary = build_session_runtime_summary(
            session,
            history_entries=[],
            codex_service_pool=[],
            claude_service_pool=[],
            gemini_service_pool=[],
            default_provider="codex",
            resident_session_ids=set(),
        )

        self.assertTrue(summary["resident_unit_session"])
        self.assertEqual(summary["associated_unit_id"], "entrance.service")
        self.assertEqual(summary["associated_template_id"], "entrance.service")
        self.assertEqual(summary["associated_unit_display_name"], "Entrance")

    def test_session_summary_exposes_user_response_request_history(self) -> None:
        session = {
            "session_id": "session-requests",
            "label": "Requests",
            "user_response_wait_active": True,
            "user_response_wait_timeout_seconds": 7200,
            "user_response_wait_effective_timeout_seconds": 300,
            "user_response_wait_started_at": "2026-05-21T09:00:00Z",
            "user_response_wait_generated_at": "2026-05-21T09:00:00Z",
            "user_response_wait_until_at": "2026-05-21T09:05:00Z",
            "user_response_wait_request_id": "user-response-123",
            "user_response_wait_prompt_text": "Which region should the worker use?",
            "user_response_wait_reason": "Deployment needs a region before it can continue.",
            "user_response_wait_requests": [
                {
                    "request_id": "user-response-123",
                    "generated_at": "2026-05-21T09:00:00Z",
                    "started_at": "2026-05-21T09:00:00Z",
                    "until_at": "2026-05-21T09:05:00Z",
                    "timeout_seconds": 7200,
                    "effective_timeout_seconds": 300,
                    "question": "Which region should the worker use?",
                    "reason": "Deployment needs a region before it can continue.",
                    "source_service_id": "service-codex-001",
                    "requested_by_role": "goal_manager",
                    "status": "waiting",
                }
            ],
        }

        summary = build_session_runtime_summary(
            session,
            history_entries=[],
            codex_service_pool=[],
            claude_service_pool=[],
            gemini_service_pool=[],
            default_provider="codex",
        )

        self.assertEqual(summary["user_response_wait_timeout_seconds"], 7200)
        self.assertEqual(summary["user_response_wait_effective_timeout_seconds"], 300)
        self.assertEqual(summary["user_response_wait_requests"][0]["request_id"], "user-response-123")
        self.assertEqual(summary["user_response_wait_requests"][0]["requested_by_role"], "goal_manager")
        self.assertEqual(summary["user_response_wait_requests"][0]["status"], "waiting")

    def test_session_registration_metadata_falls_back_to_updated_at(self) -> None:
        metadata = session_registration_metadata(
            {
                "created_at": "2026-05-18T10:00:00Z",
                "updated_at": "2026-05-18T11:00:00Z",
            }
        )

        self.assertEqual(metadata["registered_at"], "2026-05-18T10:00:00Z")
        self.assertEqual(metadata["goal_updated_at"], "2026-05-18T11:00:00Z")
        self.assertFalse(metadata["resident_unit_session"])
        self.assertFalse(metadata["has_associated_unit_file"])

    def test_session_registration_metadata_marks_registered_unit_session_resident(self) -> None:
        metadata = session_registration_metadata(
            {
                "session_id": "session-registered",
                "created_at": "2026-05-18T10:00:00Z",
                "updated_at": "2026-05-18T11:00:00Z",
                "launcher_unit_id": "entrance.service",
            },
            resident_session_ids={"session-registered"},
        )

        self.assertTrue(metadata["resident_unit_session"])
        self.assertTrue(metadata["has_associated_unit_file"])

    def test_session_registration_metadata_marks_interface_service_unit_sessions_resident(self) -> None:
        metadata = session_registration_metadata(
            {
                "session_id": "session-entrance-child",
                "created_at": "2026-05-18T10:00:00Z",
                "updated_at": "2026-05-18T11:00:00Z",
                "session_group": "user",
                "session_ui_mode": "communication",
                "launcher_unit_id": "entrance.service",
                "launcher_template_id": "entrance.service",
                "launcher_display_name": "Entrance",
                "launcher_unit_kind": "interface",
                "launcher_unit_class": "service",
                "launcher_instance_policy": "multi",
            }
        )

        self.assertTrue(metadata["resident_unit_session"])
        self.assertTrue(metadata["has_associated_unit_file"])
        self.assertEqual(metadata["associated_unit_id"], "entrance.service")
        self.assertEqual(metadata["associated_unit_display_name"], "Entrance")

    def test_existing_entrance_communication_session_metadata_is_resident(self) -> None:
        metadata = session_registration_metadata(
            {
                "session_id": "session-existing-entrance",
                "created_at": "2026-05-18T10:00:00Z",
                "updated_at": "2026-05-18T11:00:00Z",
                "session_group": "user",
                "session_ui_mode": "communication",
                "launcher_unit_id": "entrance.service",
                "launcher_template_id": "entrance.service",
                "launcher_display_name": "Entrance",
            }
        )

        self.assertTrue(metadata["resident_unit_session"])
        self.assertEqual(metadata["associated_unit_display_name"], "Entrance")

    def test_session_agent_assignment_counts_track_reviewers_separately(self) -> None:
        counts = session_agent_assignment_counts(
            {
                "service_id": "service-codex-001",
                "welcomed_agents": [
                    {"agent_id": "service-codex-001@@session-1@@interactive_agent", "join_role": "interactive_agent"},
                    {"agent_id": "service-codex-001@@session-1@@goal_manager", "join_role": "goal_manager"},
                ],
            },
            worker={"service_id": "service-codex-002"},
            goal_manager_worker={"service_id": "service-codex-003"},
            goal_manager_state="running",
        )

        self.assertEqual(counts["assigned_agents"], 3)
        self.assertEqual(counts["goal_manager_reviewers"], 2)


if __name__ == "__main__":
    unittest.main()
