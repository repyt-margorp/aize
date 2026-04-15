from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from runtime.persistent_state_pkg import create_conversation_session, get_session_settings, list_sessions
from runtime.ws_peer_client import ensure_local_proxy_session, run_ws_peer_client, ws_proxy_goal_text


class WsPeerClientProxySessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.runtime_root = Path(self.tempdir.name)
        self.username = "repyt"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_ensure_local_proxy_session_creates_fresh_session_when_name_matches_but_remote_target_differs(self) -> None:
        existing = create_conversation_session(
            self.runtime_root,
            username=self.username,
            label="[ws-proxy] remote-upstream",
        )
        assert existing is not None
        existing_id = str(existing["session_id"])

        sid, reused = ensure_local_proxy_session(
            self.runtime_root,
            local_username=self.username,
            local_session_id="",
            name="remote-upstream",
            target_ws_url="wss://example.test/ws",
            remote_username="repyt",
            remote_session_id="session-a",
        )
        self.assertNotEqual(sid, existing_id)
        self.assertFalse(reused)

        fresh_sid, reused_fresh = ensure_local_proxy_session(
            self.runtime_root,
            local_username=self.username,
            local_session_id="",
            name="remote-upstream",
            target_ws_url="wss://example.test/ws",
            remote_username="repyt",
            remote_session_id="session-b",
        )
        self.assertIsNotNone(fresh_sid)
        self.assertNotEqual(fresh_sid, existing_id)
        self.assertFalse(reused_fresh)

        talk = get_session_settings(self.runtime_root, username=self.username, session_id=fresh_sid or "")
        self.assertIsNotNone(talk)
        assert talk is not None
        self.assertEqual(
            str(talk.get("goal_text") or ""),
            ws_proxy_goal_text(
                name="remote-upstream",
                target_ws_url="wss://example.test/ws",
                remote_username="repyt",
                remote_session_id="session-b",
            ),
        )

    def test_ensure_local_proxy_session_honors_explicit_local_session_id(self) -> None:
        existing = create_conversation_session(
            self.runtime_root,
            username=self.username,
            label="[ws-proxy] remote-upstream",
        )
        assert existing is not None
        existing_id = str(existing["session_id"])

        sid, reused = ensure_local_proxy_session(
            self.runtime_root,
            local_username=self.username,
            local_session_id=existing_id,
            name="remote-upstream",
            target_ws_url="wss://example.test/ws",
            remote_username="repyt",
            remote_session_id="session-explicit",
        )
        self.assertEqual(sid, existing_id)
        self.assertFalse(reused)
        talk = get_session_settings(self.runtime_root, username=self.username, session_id=existing_id)
        self.assertEqual(
            str((talk or {}).get("goal_text") or ""),
            ws_proxy_goal_text(
                name="remote-upstream",
                target_ws_url="wss://example.test/ws",
                remote_username="repyt",
                remote_session_id="session-explicit",
            ),
        )

    def test_ensure_local_proxy_session_finds_matching_session_by_goal_fingerprint(self) -> None:
        expected_goal = ws_proxy_goal_text(
            name="remote-upstream",
            target_ws_url="wss://example.test/ws",
            remote_username="repyt",
            remote_session_id="session-z",
        )
        session = create_conversation_session(
            self.runtime_root,
            username=self.username,
            label="[ws-proxy] remote-upstream",
        )
        assert session is not None
        existing_id = str(session["session_id"])

        sid, _ = ensure_local_proxy_session(
            self.runtime_root,
            local_username=self.username,
            local_session_id=existing_id,
            name="remote-upstream",
            target_ws_url="wss://example.test/ws",
            remote_username="repyt",
            remote_session_id="session-z",
        )
        self.assertEqual(sid, existing_id)

        reused_sid, reused = ensure_local_proxy_session(
            self.runtime_root,
            local_username=self.username,
            local_session_id="",
            name="remote-upstream",
            target_ws_url="wss://example.test/ws",
            remote_username="repyt",
            remote_session_id="session-z",
        )
        self.assertEqual(reused_sid, existing_id)
        self.assertTrue(reused)
        sessions = list_sessions(self.runtime_root, username=self.username)
        self.assertEqual(len(sessions), 1)
        talk = get_session_settings(self.runtime_root, username=self.username, session_id=existing_id)
        self.assertEqual(str((talk or {}).get("goal_text") or ""), expected_goal)

    def test_source_supports_gemini_provider_pool_selection(self) -> None:
        source = Path(ROOT / "src" / "runtime" / "ws_peer_client.py").read_text(encoding="utf-8")
        self.assertIn('provider           str    "claude" | "codex" | "gemini"  (default "claude")', source)
        self.assertIn('"gemini": gemini_service_pool', source)


if __name__ == "__main__":
    unittest.main()
