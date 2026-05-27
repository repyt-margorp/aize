from __future__ import annotations

import json
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
from runtime.persistent_state_pkg.agent_audit import save_agent_audit_state
from runtime.persistent_state_pkg._core import session_goal_manager_state_path, session_metadata_path, write_json_file
import runtime.session_view as session_view
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

    def test_list_sessions_clears_due_user_response_wait_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / ".aize-runtime"
            runtime_root.mkdir(parents=True)
            session = create_conversation_session(runtime_root, username="root", label="Timed Out Wait")
            session_id = str(session["session_id"])
            session_path = session_metadata_path(runtime_root, username="root", session_id=session_id)
            stored = json.loads(session_path.read_text())
            stored["user_response_wait_active"] = True
            stored["user_response_wait_request_id"] = "req-timeout"
            stored["user_response_wait_until_at"] = "2026-05-21T09:05:00Z"
            stored["user_response_wait_requests"] = [
                {
                    "request_id": "req-timeout",
                    "status": "waiting",
                    "until_at": "2026-05-21T09:05:00Z",
                }
            ]
            write_json_file(session_path, stored)

            sessions = list_sessions(runtime_root, username="root")
            refreshed = next(item for item in sessions if item["session_id"] == session_id)

            self.assertFalse(refreshed["user_response_wait_active"])
            self.assertTrue(refreshed["user_response_wait_last_timeout_at"])
            self.assertEqual(refreshed["user_response_wait_requests"][-1]["status"], "timed_out")

    def test_session_summary_prefers_live_agent_audit_state_over_stale_session_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / ".aize-runtime"
            runtime_root.mkdir(parents=True)
            session = create_conversation_session(runtime_root, username="root", label="Runtime")
            session_id = str(session["session_id"])
            save_agent_audit_state(
                runtime_root,
                service_id="service-codex-001",
                username="root",
                session_id=session_id,
                audit_state="all_clear",
            )

            summary = build_session_runtime_summary(
                {
                    **session,
                    "service_id": "service-codex-001",
                    "goal_audit_state": "panic",
                },
                history_entries=[],
                codex_service_pool=["service-codex-001"],
                claude_service_pool=[],
                gemini_service_pool=[],
                default_provider="codex",
                runtime_root=runtime_root,
                username="root",
            )

            self.assertEqual(summary["goal_audit_state"], "all_clear")

    def test_session_summary_prefers_goal_manager_all_clear_over_stale_worker_panic(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / ".aize-runtime"
            runtime_root.mkdir(parents=True)
            session = create_conversation_session(runtime_root, username="root", label="Runtime")
            session_id = str(session["session_id"])
            save_agent_audit_state(
                runtime_root,
                service_id="service-codex-001",
                username="root",
                session_id=session_id,
                audit_state="panic",
            )
            write_json_file(
                session_goal_manager_state_path(runtime_root, username="root", session_id=session_id),
                {
                    "state": "idle",
                    "service_id": "service-codex-007",
                    "progress_state": "in_progress",
                    "audit_state": "all_clear",
                    "updated_at": "2026-05-21T16:02:43Z",
                },
            )

            summary = build_session_runtime_summary(
                {
                    **session,
                    "service_id": "service-codex-001",
                    "goal_active": True,
                    "goal_completed": False,
                    "goal_progress_state": "in_progress",
                },
                history_entries=[],
                codex_service_pool=["service-codex-001"],
                claude_service_pool=[],
                gemini_service_pool=[],
                default_provider="codex",
                runtime_root=runtime_root,
                username="root",
            )

            self.assertEqual(summary["goal_audit_state"], "all_clear")
            self.assertEqual(summary["runtime_execution_state"], "idle")

    def test_session_summary_uses_persisted_queued_goal_manager_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / ".aize-runtime"
            runtime_root.mkdir(parents=True)
            session = create_conversation_session(runtime_root, username="root", label="Runtime")
            session_id = str(session["session_id"])
            write_json_file(
                session_goal_manager_state_path(runtime_root, username="root", session_id=session_id),
                {
                    "state": "queued",
                    "service_id": "service-codex-007",
                    "progress_state": "in_progress",
                    "audit_state": "all_clear",
                    "pending_work_items": [{"kind": "lifecycle_owner_lost"}],
                    "updated_at": "2026-05-23T13:58:00Z",
                },
            )

            summary = build_session_runtime_summary(
                {
                    **session,
                    "service_id": "service-codex-001",
                    "goal_active": True,
                    "goal_completed": False,
                    "goal_progress_state": "in_progress",
                },
                history_entries=[],
                codex_service_pool=["service-codex-001", "service-codex-007"],
                claude_service_pool=[],
                gemini_service_pool=[],
                default_provider="codex",
                runtime_root=runtime_root,
                username="root",
            )

            self.assertEqual(summary["goal_manager_state"], "queued")
            self.assertEqual(summary["goal_manager_worker"]["service_id"], "service-codex-007")
            self.assertEqual(summary["runtime_execution_state"], "running")
            self.assertEqual(summary["goal_manager_reviewer_count"], 1)

    def test_session_summary_skips_reconcile_for_read_only_views(self) -> None:
        session = {
            "session_id": "session-1",
            "label": "Runtime",
            "service_id": "service-codex-001",
            "goal_active": True,
            "goal_completed": False,
            "goal_progress_state": "in_progress",
        }
        audit_calls: list[bool] = []
        gm_calls: list[bool] = []

        def fake_audit_summary(*args, **kwargs):
            audit_calls.append(bool(kwargs.get("allow_reconcile", True)))
            return {"audit_state": "all_clear"}

        def fake_goal_manager(*args, **kwargs):
            gm_calls.append(bool(kwargs.get("allow_reconcile", True)))
            return {"state": "idle", "service_id": "service-codex-001"}

        with patch.object(session_view, "load_session_audit_summary", side_effect=fake_audit_summary), patch.object(
            session_view, "persisted_goal_manager_runtime_state", side_effect=fake_goal_manager
        ):
            summary = build_session_runtime_summary(
                session,
                history_entries=[],
                codex_service_pool=["service-codex-001"],
                claude_service_pool=[],
                gemini_service_pool=[],
                default_provider="codex",
                runtime_root=Path("/tmp/runtime"),
                username="root",
                allow_reconcile=False,
            )

        self.assertEqual(summary["goal_audit_state"], "all_clear")
        self.assertEqual(audit_calls, [False])
        self.assertEqual(gm_calls, [False])

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
                "goal_active": True,
                "goal_progress_state": "in_progress",
                "goal_completed": False,
                "agent_running": True,
                "welcomed_agents": [
                    {"agent_id": "service-codex-001@@session-1@@interactive_agent", "join_role": "interactive_agent"},
                    {"agent_id": "service-codex-001@@session-1@@goal_manager", "join_role": "goal_manager"},
                ],
            },
            worker={"service_id": "service-codex-002"},
            goal_manager_worker={"service_id": "service-codex-003"},
            goal_manager_state="running",
        )

        self.assertEqual(counts["assigned_agents"], 2)
        self.assertEqual(counts["goal_manager_reviewers"], 2)

    def test_session_agent_assignment_counts_include_idle_assignments(self) -> None:
        counts = session_agent_assignment_counts(
            {
                "service_id": "service-codex-001",
                "goal_active": True,
                "goal_progress_state": "in_progress",
                "goal_completed": False,
                "agent_running": False,
                "welcomed_agents": [
                    {"service_id": "service-codex-001", "join_role": "worker_agent"},
                    {"service_id": "service-codex-002", "join_role": "interactive_agent"},
                    {"service_id": "service-codex-003", "join_role": "goal_manager"},
                ],
            },
        )

        self.assertEqual(counts["assigned_agents"], 2)
        self.assertEqual(counts["goal_manager_reviewers"], 1)


if __name__ == "__main__":
    unittest.main()
