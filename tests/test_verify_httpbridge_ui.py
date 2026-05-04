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
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.diagnostics import verify_httpbridge_ui


class VerifyHttpBridgeUiTests(unittest.TestCase):
    def test_needs_direct_http_fallback_for_parent_session_failure(self) -> None:
        self.assertTrue(
            verify_httpbridge_ui.needs_direct_http_fallback(
                {"ok": False, "error": 'session_create_failed:{"status":404,"payload":{"error":"parent_session_not_found"}}'},
                session_token="token",
            )
        )
        self.assertTrue(
            verify_httpbridge_ui.needs_direct_http_fallback(
                {"ok": False, "error": "chrome_timeout:13"},
                session_token="token",
            )
        )
        self.assertFalse(
            verify_httpbridge_ui.needs_direct_http_fallback(
                {"ok": True},
                session_token="token",
            )
        )

    def test_main_falls_back_to_direct_http_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / ".aize-runtime"
            runtime_root.mkdir(parents=True)
            initial_probe_result = {
                "ok": False,
                "error": 'login_failed:{"status":401,"payload":{"error":"auth_required_or_invalid_session"}}',
                "chrome_bin": "/usr/bin/google-chrome",
            }
            token_probe_result = {
                "ok": False,
                "error": 'session_create_failed:{"status":404,"payload":{"error":"parent_session_not_found"}}',
                "chrome_bin": "/usr/bin/google-chrome",
            }
            direct_result = {
                "ok": True,
                "provider": "codex",
                "session_markers": {"session_map": True, "workspace_history": True, "nodes": True, "requests": True, "goal_editor": True},
                "child_markers": {"session_map": True, "workspace_history": True, "nodes": True, "requests": True, "goal_editor": True},
                "created_session_id": "child-1",
                "goal_update_status": 200,
                "provider_update_status": 200,
                "effective_provider": "codex",
                "prompt_send_status": 202,
                "prompt_provider": "codex",
                "persisted_goal_text": verify_httpbridge_ui.TARGET_GOAL_TEXT,
                "target_goal_text": verify_httpbridge_ui.TARGET_GOAL_TEXT,
                "root_status": 200,
                "session_create_status": 201,
                "child_page_status": 200,
                "verification_mode": "http_api",
            }
            with (
                patch.object(verify_httpbridge_ui, "run_probe", side_effect=[initial_probe_result, token_probe_result]) as run_probe,
                patch.object(verify_httpbridge_ui, "maybe_mint_local_session_token", return_value="minted-token") as mint_token,
                patch.object(verify_httpbridge_ui, "run_direct_verification", return_value=direct_result) as run_direct,
                patch.object(sys, "argv", [
                    "verify_httpbridge_ui.py",
                    "--runtime-root",
                    str(runtime_root),
                    "--provider",
                    "codex",
                ]),
            ):
                exit_code = verify_httpbridge_ui.main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(run_probe.call_count, 2)
        mint_token.assert_called_once()
        run_direct.assert_called_once()
        self.assertEqual(run_direct.call_args.kwargs["session_token"], "minted-token")


if __name__ == "__main__":
    unittest.main()
