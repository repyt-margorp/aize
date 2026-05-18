from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from runtime.persistent_state_pkg import create_conversation_session, ensure_state
from runtime.persistent_state_pkg.provider_session import (
    list_claude_sessions,
    list_codex_sessions,
    list_gemini_sessions,
    load_claude_session,
    load_codex_session,
    load_gemini_session,
    save_claude_session,
    save_codex_session,
    save_gemini_session,
)


class ProviderSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.runtime_root = Path(self.tempdir.name) / "runtime"
        ensure_state(self.runtime_root)
        self.username = "test-user"
        self.session = create_conversation_session(self.runtime_root, username=self.username, label="Test Session")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_save_and_load_provider_sessions_per_provider(self) -> None:
        provider_cases = [
            (
                save_codex_session,
                load_codex_session,
                "service-codex-001",
                "codex-thread",
            ),
            (
                save_claude_session,
                load_claude_session,
                "service-claude-001",
                "claude-thread",
            ),
            (
                save_gemini_session,
                load_gemini_session,
                "service-gemini-001",
                "gemini-thread",
            ),
        ]

        for save_session, load_session, service_id, provider_session_id in provider_cases:
            with self.subTest(provider_session_id=provider_session_id):
                save_session(
                    self.runtime_root,
                    service_id=service_id,
                    provider_session_id=provider_session_id,
                    username=self.username,
                    session_id=self.session["session_id"],
                    session_slot="interactive_agent",
                )

                self.assertEqual(
                    load_session(
                        self.runtime_root,
                        service_id=service_id,
                        username=self.username,
                        session_id=self.session["session_id"],
                        slot="interactive_agent",
                    ),
                    provider_session_id,
                )

    def test_list_provider_sessions_deduplicates_by_slot(self) -> None:
        session_two = create_conversation_session(self.runtime_root, username=self.username, label="Session Two")

        save_codex_session(
            self.runtime_root,
            service_id="service-codex-001",
            provider_session_id="worker-thread",
            username=self.username,
            session_id=self.session["session_id"],
            slot="worker_agent",
        )
        save_codex_session(
            self.runtime_root,
            service_id="service-codex-001",
            provider_session_id="interactive-thread",
            username=self.username,
            session_id=self.session["session_id"],
            slot="interactive_agent",
        )
        save_codex_session(
            self.runtime_root,
            service_id="service-codex-001",
            provider_session_id="second-thread",
            username=self.username,
            session_id=session_two["session_id"],
            slot="worker_agent",
        )

        sessions = sorted(
            list_codex_sessions(self.runtime_root, service_id="service-codex-001"),
            key=lambda item: (item["conversation_session_id"], item["slot"]),
        )

        expected = sorted(
            [
                {
                    "username": self.username,
                    "conversation_session_id": self.session["session_id"],
                    "slot": "worker_agent",
                    "session_id": "worker-thread",
                },
                {
                    "username": self.username,
                    "conversation_session_id": session_two["session_id"],
                    "slot": "worker_agent",
                    "session_id": "second-thread",
                },
                {
                    "username": self.username,
                    "conversation_session_id": self.session["session_id"],
                    "slot": "interactive_agent",
                    "session_id": "interactive-thread",
                },
            ],
            key=lambda item: (item["conversation_session_id"], item["slot"]),
        )

        self.assertEqual(
            sessions,
            expected,
        )

    def test_list_provider_sessions_are_scoped_to_provider(self) -> None:
        save_claude_session(
            self.runtime_root,
            service_id="service-mixed-001",
            provider_session_id="claude-thread",
            username=self.username,
            session_id=self.session["session_id"],
        )
        save_gemini_session(
            self.runtime_root,
            service_id="service-mixed-001",
            provider_session_id="gemini-thread",
            username=self.username,
            session_id=self.session["session_id"],
        )

        self.assertEqual(
            list_claude_sessions(self.runtime_root, service_id="service-mixed-001"),
            [
                {
                    "username": self.username,
                    "conversation_session_id": self.session["session_id"],
                    "slot": "worker_agent",
                    "session_id": "claude-thread",
                }
            ],
        )
        self.assertEqual(
            list_gemini_sessions(self.runtime_root, service_id="service-mixed-001"),
            [
                {
                    "username": self.username,
                    "conversation_session_id": self.session["session_id"],
                    "slot": "worker_agent",
                    "session_id": "gemini-thread",
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
