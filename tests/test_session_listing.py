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
from runtime.session_view import build_session_runtime_summary, session_registration_metadata


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


if __name__ == "__main__":
    unittest.main()
