from __future__ import annotations

import json
from pathlib import Path
import os
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
    def test_maybe_mint_local_session_token_uses_canonical_state_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / ".aize-runtime"
            runtime_root.mkdir(parents=True)
            canonical_state_dir = Path(tmpdir) / ".aize-state"
            canonical_state_dir.mkdir(parents=True)
            (canonical_state_dir / "persistent.json").write_text("{}", encoding="utf-8")
            with patch.object(verify_httpbridge_ui, "create_session", return_value="minted-token") as create_session:
                token = verify_httpbridge_ui.maybe_mint_local_session_token(runtime_root, username="root")

        self.assertEqual(token, "minted-token")
        create_session.assert_called_once_with(runtime_root, username="root")

    def test_run_direct_verification_sends_parent_session_as_session_context(self) -> None:
        calls: list[dict] = []

        def fake_http_request(**kwargs):
            calls.append(kwargs)
            path = kwargs["path"]
            if path == "/session/select":
                return 200, "", {}, ""
            if path == "/":
                return 200, "<div id='session-map-pane'></div><div id='messages'></div><div id='workspace-view'></div>", {}, ""
            if path == "/session/select":
                return 200, "", {}, ""
            if path == "/sessions":
                return 201, "", {"active_session_id": "child-1", "session": {"session_id": "child-1"}}, ""
            if path == "/session/goal":
                return 200, "", {}, ""
            if path == "/session/goal/state":
                return 200, "", {"preferred_provider": "codex"}, ""
            if path == "/message":
                return 202, "", {"provider": "codex"}, ""
            if path.startswith("/?session_id="):
                return 200, "<div id='session-map-pane'></div><div id='messages'></div><div id='workspace-view'></div>", {}, ""
            raise AssertionError(path)

        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / ".aize-runtime"
            runtime_root.mkdir(parents=True)
            session_dir = runtime_root.parent / ".aize-state" / "sessions" / "root" / "child-1"
            session_dir.mkdir(parents=True)
            (session_dir / "session.json").write_text(
                '{"goal_text":"Verify HTTPBridge goal save flow updated"}',
                encoding="utf-8",
            )
            with patch.object(verify_httpbridge_ui, "http_request", side_effect=fake_http_request):
                result = verify_httpbridge_ui.run_direct_verification(
                    runtime_root=runtime_root,
                    base_url="https://127.0.0.1:4123",
                    session_token="token",
                    parent_session_id="parent-1",
                    provider="codex",
                    password="unused",
                    username="root",
                )

        self.assertTrue(result["ok"])
        sessions_call = next(call for call in calls if call["path"] == "/sessions")
        self.assertEqual(
            sessions_call["payload"],
            {
                "label": "UI Verify Child",
                "session_id": "parent-1",
                "parent_session_id": "parent-1",
            },
        )

    def test_resolve_base_url_candidates_prefers_active_runtime_port_and_omits_stale_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / ".aize-runtime"
            (runtime_root / "state").mkdir(parents=True)
            (runtime_root / "state" / "services.json").write_text(
                '{"services":{"service-http-001":{"status":"running","current_process_id":"proc-live","config":{"port":64123}}}}',
                encoding="utf-8",
            )
            with (
                patch.dict(os.environ, {}, clear=True),
                patch.object(
                    verify_httpbridge_ui,
                    "_fetch_health_payload",
                    side_effect=lambda url: (
                        {"ok": True, "process_id": "proc-stale"}
                        if (url.endswith(":4123/health") or url.endswith(":4123"))
                        else (
                            {"ok": True, "process_id": "proc-live"}
                            if (url.endswith(":64123/health") or url.endswith(":64123"))
                            else {}
                        )
                    ),
                ),
            ):
                resolved = verify_httpbridge_ui.resolve_base_url_candidates(runtime_root)

        self.assertEqual(resolved, ["https://127.0.0.1:64123"])

    def test_resolve_base_url_candidates_keeps_default_when_state_marks_service_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / ".aize-runtime"
            (runtime_root / "state").mkdir(parents=True)
            (runtime_root / "state" / "services.json").write_text(
                '{"services":{"service-http-001":{"status":"failed","current_process_id":"proc-failed","config":{"port":64123}}}}',
                encoding="utf-8",
            )
            with (
                patch.dict(os.environ, {}, clear=True),
                patch.object(
                    verify_httpbridge_ui,
                    "_fetch_health_payload",
                    side_effect=lambda url: (
                        {"ok": True, "process_id": "proc-prior"}
                        if (url.endswith(":4123/health") or url.endswith(":4123"))
                        else {}
                    ),
                ),
            ):
                resolved = verify_httpbridge_ui.resolve_base_url_candidates(runtime_root)

        self.assertIn("https://127.0.0.1:4123", resolved)

    def test_extract_result_json_accepts_single_quoted_result_id(self) -> None:
        payload = {"ok": True, "provider": "codex"}
        dom = "<!doctype html><pre id='result'>" + verify_httpbridge_ui.json.dumps(payload) + "</pre>"
        self.assertEqual(verify_httpbridge_ui.extract_result_json(dom), payload)

    def test_resolve_probe_parent_session_id_prefers_top_level_non_probe_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / ".aize-runtime"
            runtime_root.mkdir(parents=True)
            sessions = [
                {
                    "session_id": "probe-child-newer",
                    "label": "UI Verify Child",
                    "updated_at": "2026-05-04T03:32:46Z",
                    "created_by_type": "user",
                    "parent_session_id": "root-parent",
                    "session_permissions": {
                        "create_child_session": True,
                        "update_goal": True,
                        "send_prompt": True,
                    },
                },
                {
                    "session_id": "root-parent",
                    "label": "Root",
                    "updated_at": "2026-05-04T03:31:00Z",
                    "created_by_type": "user",
                    "parent_session_id": "",
                    "session_permissions": {
                        "create_child_session": True,
                        "update_goal": False,
                        "send_prompt": False,
                    },
                },
            ]
            with patch.object(verify_httpbridge_ui, "list_sessions", return_value=sessions):
                resolved = verify_httpbridge_ui.resolve_probe_parent_session_id(
                    runtime_root,
                    username="root",
                )

        self.assertEqual(resolved, "root-parent")

    def test_resolve_base_url_falls_back_to_default_listener_when_running_state_port_is_unreachable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / ".aize-runtime"
            (runtime_root / "state").mkdir(parents=True)
            (runtime_root / "state" / "services.json").write_text(
                '{"services":{"service-http-001":{"status":"running","config":{"port":64123}}}}',
                encoding="utf-8",
            )
            with (
                patch.dict(
                    os.environ,
                    {},
                    clear=True,
                ),
                patch.object(
                    verify_httpbridge_ui,
                    "_fetch_health_payload",
                    side_effect=lambda url: {"ok": True, "process_id": "proc-default"} if url.endswith(":4123/health") or url.endswith(":4123") else {},
                ),
            ):
                resolved = verify_httpbridge_ui.resolve_base_url(runtime_root)

        self.assertEqual(resolved, "https://127.0.0.1:64123")

    def test_resolve_base_url_prefers_running_state_port_when_process_id_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / ".aize-runtime"
            (runtime_root / "state").mkdir(parents=True)
            (runtime_root / "state" / "services.json").write_text(
                '{"services":{"service-http-001":{"status":"running","current_process_id":"proc-live","config":{"port":64123}}}}',
                encoding="utf-8",
            )
            with (
                patch.dict(os.environ, {}, clear=True),
                patch.object(
                    verify_httpbridge_ui,
                    "_fetch_health_payload",
                    side_effect=lambda url: (
                        {"ok": True, "process_id": "proc-stale"}
                        if url.endswith(":4123/health")
                        else (
                            {"ok": True, "process_id": "proc-live"}
                            if url.endswith(":64123/health")
                            else {}
                        )
                    ),
                ),
            ):
                resolved = verify_httpbridge_ui.resolve_base_url(runtime_root)

        self.assertEqual(resolved, "https://127.0.0.1:64123")

    def test_resolve_base_url_ignores_failed_service_state_port(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / ".aize-runtime"
            (runtime_root / "state").mkdir(parents=True)
            (runtime_root / "state" / "services.json").write_text(
                '{"services":{"service-http-001":{"status":"failed","config":{"port":64123}}}}',
                encoding="utf-8",
            )
            with (
                patch.dict(
                    os.environ,
                    {},
                    clear=True,
                ),
                patch.object(
                    verify_httpbridge_ui,
                    "_fetch_health_payload",
                    side_effect=lambda url: {"ok": True, "process_id": "proc-any"} if (url.endswith(":4123/health") or url.endswith(":4123") or url.endswith(":64123/health") or url.endswith(":64123")) else {},
                ),
            ):
                resolved = verify_httpbridge_ui.resolve_base_url(runtime_root)

        self.assertEqual(resolved, "https://127.0.0.1:64123")

    def test_resolve_base_url_prefers_running_state_port_when_it_is_healthy_without_process_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / ".aize-runtime"
            (runtime_root / "state").mkdir(parents=True)
            (runtime_root / "state" / "services.json").write_text(
                '{"services":{"service-http-001":{"status":"running","current_process_id":"proc-live","config":{"port":64123}}}}',
                encoding="utf-8",
            )
            with (
                patch.dict(os.environ, {}, clear=True),
                patch.object(
                    verify_httpbridge_ui,
                    "_fetch_health_payload",
                    side_effect=lambda url: (
                        {"ok": True, "process_id": "proc-stale"}
                        if url.endswith(":4123/health")
                        else (
                            {"ok": True}
                            if url.endswith(":64123/health")
                            else {}
                        )
                    ),
                ),
            ):
                resolved = verify_httpbridge_ui.resolve_base_url(runtime_root)

        self.assertEqual(resolved, "https://127.0.0.1:64123")

    def test_resolve_base_url_prefers_running_state_port_when_both_are_live(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / ".aize-runtime"
            (runtime_root / "state").mkdir(parents=True)
            (runtime_root / "state" / "services.json").write_text(
                '{"services":{"service-http-001":{"status":"running","config":{"port":64123},"current_process_id":"proc-live"}}}',
                encoding="utf-8",
            )
            with (
                patch.dict(
                    os.environ,
                    {},
                    clear=True,
                ),
                patch.object(
                    verify_httpbridge_ui,
                    "_fetch_health_payload",
                    side_effect=lambda url: (
                        {"ok": True, "process_id": "proc-live"} if (url.endswith(":4123/health") or url.endswith(":4123")) else
                        ({"ok": True, "process_id": "proc-live"} if (url.endswith(":64123/health") or url.endswith(":64123")) else {})
                    ),
                ),
            ):
                resolved = verify_httpbridge_ui.resolve_base_url(runtime_root)

        self.assertEqual(resolved, "https://127.0.0.1:64123")

    def test_resolve_base_url_prefers_env_port_when_default_is_live(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / ".aize-runtime"
            (runtime_root / "state").mkdir(parents=True)
            with (
                patch.dict(
                    os.environ,
                    {"AIZE_HTTP_PORT": "64123", "AIZE_TLS": "true"},
                    clear=True,
                ),
                patch.object(
                    verify_httpbridge_ui,
                    "_fetch_health_payload",
                    side_effect=lambda url: {"ok": True, "process_id": "proc-env"} if (url.endswith(":4123/health") or url.endswith(":4123") or url.endswith(":64123/health") or url.endswith(":64123")) else {},
                ),
            ):
                resolved = verify_httpbridge_ui.resolve_base_url(runtime_root)

        self.assertEqual(resolved, "https://127.0.0.1:64123")

    def test_resolve_base_url_ignores_healthy_listener_with_wrong_process_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / ".aize-runtime"
            (runtime_root / "state").mkdir(parents=True)
            (runtime_root / "state" / "services.json").write_text(
                '{"services":{"service-http-001":{"status":"running","config":{"port":64123},"current_process_id":"proc-live"}}}',
                encoding="utf-8",
            )
            with (
                patch.dict(
                    os.environ,
                    {},
                    clear=True,
                ),
                patch.object(
                    verify_httpbridge_ui,
                    "_fetch_health_payload",
                    side_effect=lambda url: (
                        {"ok": True, "process_id": "proc-stale"} if (url.endswith(":4123/health") or url.endswith(":4123")) else
                        ({"ok": True, "process_id": "proc-live"} if (url.endswith(":64123/health") or url.endswith(":64123")) else {})
                    ),
                ),
            ):
                resolved = verify_httpbridge_ui.resolve_base_url(runtime_root)

        self.assertEqual(resolved, "https://127.0.0.1:64123")

    def test_needs_direct_http_fallback_for_parent_session_failure(self) -> None:
        self.assertTrue(
            verify_httpbridge_ui.needs_direct_http_fallback(
                {"ok": False, "error": "chrome_timeout:13"},
                session_token="",
            )
        )
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

    def test_http_request_returns_structured_timeout_result(self) -> None:
        with patch.object(verify_httpbridge_ui, "urlopen", side_effect=TimeoutError):
            status, text, payload, cookie = verify_httpbridge_ui.http_request(
                base_url="https://127.0.0.1:4123",
                path="/",
            )

        self.assertEqual(status, 598)
        self.assertEqual(text, "")
        self.assertEqual(payload, {"error": "http_timeout"})
        self.assertEqual(cookie, "")

    def test_run_direct_verification_reauths_when_root_page_lacks_ui_markers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / ".aize-runtime"
            runtime_root.mkdir(parents=True)
            session_dir = runtime_root.parent / ".aize-state" / "sessions" / "root" / "child-1"
            session_dir.mkdir(parents=True)
            (session_dir / "session.json").write_text(
                '{"goal_text":"Verify HTTPBridge goal save flow updated"}',
                encoding="utf-8",
            )
            http_calls: list[tuple[str, str]] = []

            def fake_http_request(**kwargs):
                path = kwargs["path"]
                token = kwargs.get("session_token", "")
                http_calls.append((path, token))
                if path == "/" or path == "/?session_id=parent-1":
                    if token == "authed-token":
                        return 200, "<div id='session-map-pane'></div><div id='messages'></div><div id='workspace-view'></div>", {}, ""
                    return 200, "<html>login</html>", {}, ""
                if path == "/sessions":
                    self.assertEqual(token, "authed-token")
                    return 201, "", {"active_session_id": "child-1"}, ""
                if path == "/session/goal":
                    return 200, "", {}, ""
                if path == "/session/goal/state":
                    return 200, "", {"preferred_provider": "claude"}, ""
                if path == "/message":
                    return 202, "", {"provider": "claude"}, ""
                if path == "/?session_id=child-1":
                    return 200, "<div id='session-map-pane'></div><div id='messages'></div><div id='workspace-view'></div>", {}, ""
                raise AssertionError(path)
            with (
                patch.object(verify_httpbridge_ui, "http_request", side_effect=fake_http_request),
                patch.object(verify_httpbridge_ui, "authenticate_http_session", return_value=("authed-token", "login")) as authenticate,
            ):
                result = verify_httpbridge_ui.run_direct_verification(
                    runtime_root=runtime_root,
                    base_url="https://127.0.0.1:4123",
                    session_token="stale-token",
                    parent_session_id="parent-1",
                    provider="claude",
                    password="ui-verify-pass",
                    username="root",
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["auth_mode"], "login")
        authenticate.assert_called_once()
        self.assertIn(("/sessions", "authed-token"), http_calls)

    def test_run_direct_verification_keeps_parent_session_when_parent_talk_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / ".aize-runtime"
            runtime_root.mkdir(parents=True)
            session_dir = runtime_root.parent / ".aize-state" / "sessions" / "root" / "child-1"
            session_dir.mkdir(parents=True)
            (session_dir / "session.json").write_text(
                '{"goal_text":"Verify HTTPBridge goal save flow updated"}',
                encoding="utf-8",
            )
            calls: list[dict] = []

            def fake_http_request(**kwargs):
                calls.append(kwargs)
                path = kwargs["path"]
                if path == "/" or path.startswith("/?session_id="):
                    return 200, "<div id='session-map-pane'></div><div id='messages'></div><div id='workspace-view'></div>", {}, ""
                if path == "/sessions":
                    payload = kwargs.get("payload") or {}
                    if "parent_session_id" in payload:
                        return 401, "", {"error": "auth_required_or_invalid_talk"}, ""
                    return 201, "", {"active_session_id": "child-1", "session": {"session_id": "child-1"}}, ""
                if path == "/session/goal":
                    return 200, "", {}, ""
                if path == "/session/goal/state":
                    return 200, "", {"preferred_provider": "codex"}, ""
                if path == "/message":
                    return 202, "", {"provider": "codex"}, ""
                raise AssertionError(path)

            with (
                patch.object(verify_httpbridge_ui, "http_request", side_effect=fake_http_request),
                patch.object(verify_httpbridge_ui, "authenticate_http_session", return_value=("", "")) as authenticate,
                patch.object(verify_httpbridge_ui, "maybe_mint_local_session_token_if_available", return_value="") as mint_token,
            ):
                result = verify_httpbridge_ui.run_direct_verification(
                    runtime_root=runtime_root,
                    base_url="https://127.0.0.1:4123",
                    session_token="token",
                    parent_session_id="parent-1",
                    provider="codex",
                    password="unused",
                    username="root",
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["parent_session_id"], "parent-1")
        self.assertEqual(result["session_create_status"], 201)
        self.assertEqual(result["session_create_error"], "")
        authenticate.assert_called_once()
        mint_token.assert_called_once_with(runtime_root, username="root", active_session_id="parent-1")
        self.assertEqual(calls[0]["path"], "/?session_id=parent-1")
        session_calls = [call for call in calls if call["path"] == "/sessions"]
        self.assertEqual(len(session_calls), 2)
        self.assertEqual(
            session_calls[0]["payload"],
            {"label": "UI Verify Child", "session_id": "parent-1", "parent_session_id": "parent-1"},
        )
        self.assertEqual(session_calls[1]["payload"], {"label": "UI Verify Child"})

    def test_main_http_api_keeps_resolved_live_port_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / ".aize-runtime"
            runtime_root.mkdir(parents=True)
            direct_result = {
                "ok": True,
                "provider": "codex",
                "session_markers": {"session_map": True, "workspace_history": True, "nodes": True, "requests": True, "goal_editor": True},
                "child_markers": {"session_map": True, "workspace_history": True, "nodes": True, "requests": True, "goal_editor": True},
                "created_session_id": "child-1",
                "goal_update_status": 202,
                "provider_update_status": 200,
                "effective_provider": "codex",
                "prompt_send_status": 202,
                "prompt_provider": "codex",
                "persisted_goal_text": verify_httpbridge_ui.TARGET_GOAL_TEXT,
                "target_goal_text": verify_httpbridge_ui.TARGET_GOAL_TEXT,
                "root_status": 200,
                "session_create_status": 201,
                "child_page_status": 200,
                "verification_mode": "http_api_full",
            }
            with (
                patch.object(
                    verify_httpbridge_ui,
                    "resolve_base_url_candidates",
                    return_value=["https://127.0.0.1:64123", "https://127.0.0.1:4123"],
                ),
                patch.object(
                    verify_httpbridge_ui,
                    "maybe_mint_local_session_token_if_available",
                    return_value="minted-token",
                ),
                patch.object(
                    verify_httpbridge_ui,
                    "run_direct_verification",
                    return_value=direct_result,
                ) as run_direct,
                patch.object(sys, "argv", [
                    "verify_httpbridge_ui.py",
                    "--runtime-root",
                    str(runtime_root),
                    "--provider",
                    "codex",
                    "--verification-mode",
                    "http_api",
                ]),
            ):
                exit_code = verify_httpbridge_ui.main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(run_direct.call_args.kwargs["base_url"], "https://127.0.0.1:64123")
        self.assertEqual(run_direct.call_args.kwargs["session_token"], "minted-token")

    def test_main_uses_direct_http_verification_without_running_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / ".aize-runtime"
            runtime_root.mkdir(parents=True)
            direct_result = {
                "ok": True,
                "provider": "codex",
                "session_markers": {"session_map": True, "workspace_history": True, "nodes": True, "requests": True, "goal_editor": True},
                "child_markers": {"session_map": True, "workspace_history": True, "nodes": True, "requests": True, "goal_editor": True},
                "created_session_id": "child-1",
                "goal_update_status": 202,
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
                patch.object(verify_httpbridge_ui, "safe_run_probe") as run_probe,
                patch.object(verify_httpbridge_ui, "maybe_mint_local_session_token_if_available", return_value="minted-token") as mint_token,
                patch.object(verify_httpbridge_ui, "run_direct_verification", return_value=direct_result) as run_direct,
                patch.object(sys, "argv", [
                    "verify_httpbridge_ui.py",
                    "--runtime-root",
                    str(runtime_root),
                    "--provider",
                    "codex",
                    "--verification-mode",
                    "http_api",
                ]),
            ):
                exit_code = verify_httpbridge_ui.main()

        self.assertEqual(exit_code, 0)
        run_probe.assert_not_called()
        mint_token.assert_called_once()
        run_direct.assert_called_once()
        self.assertEqual(run_direct.call_args.kwargs["session_token"], "minted-token")

    def test_main_uses_direct_http_verification_for_explicit_parent_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / ".aize-runtime"
            runtime_root.mkdir(parents=True)
            direct_result = {
                "ok": True,
                "provider": "codex",
                "session_markers": {"session_map": True, "workspace_history": True, "nodes": True, "requests": True, "goal_editor": True},
                "child_markers": {"session_map": True, "workspace_history": True, "nodes": True, "requests": True, "goal_editor": True},
                "created_session_id": "child-explicit",
                "goal_update_status": 202,
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
                "parent_session_id": "parent-123",
            }
            with (
                patch.object(verify_httpbridge_ui, "safe_run_probe") as run_probe,
                patch.object(verify_httpbridge_ui, "maybe_mint_local_session_token_if_available", return_value="minted-token") as mint_token,
                patch.object(verify_httpbridge_ui, "run_direct_verification", return_value=direct_result) as run_direct,
                patch.object(sys, "argv", [
                    "verify_httpbridge_ui.py",
                    "--runtime-root",
                    str(runtime_root),
                    "--provider",
                    "codex",
                    "--parent-session-id",
                    "parent-123",
                ]),
            ):
                exit_code = verify_httpbridge_ui.main()

        self.assertEqual(exit_code, 0)
        run_probe.assert_not_called()
        mint_token.assert_called_once_with(runtime_root, username="root", active_session_id="parent-123")
        run_direct.assert_called_once()
        self.assertEqual(run_direct.call_args.kwargs["session_token"], "minted-token")
        self.assertEqual(run_direct.call_args.kwargs["parent_session_id"], "parent-123")

    def test_main_direct_http_retries_alternate_base_url_after_split_listener_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / ".aize-runtime"
            runtime_root.mkdir(parents=True)
            failed_result = {
                "ok": False,
                "root_status": 598,
                "session_create_status": 401,
                "created_session_id": "",
                "verification_mode": "http_api",
            }
            passed_result = {
                "ok": True,
                "provider": "codex",
                "session_markers": {"session_map": True, "workspace_history": True, "nodes": True, "requests": True, "goal_editor": True},
                "child_markers": {"session_map": True, "workspace_history": True, "nodes": True, "requests": True, "goal_editor": True},
                "created_session_id": "child-2",
                "goal_update_status": 202,
                "provider_update_status": 200,
                "effective_provider": "codex",
                "prompt_send_status": 202,
                "prompt_provider": "codex",
                "persisted_goal_text": verify_httpbridge_ui.TARGET_GOAL_TEXT,
                "target_goal_text": verify_httpbridge_ui.TARGET_GOAL_TEXT,
                "root_status": 200,
                "session_create_status": 201,
                "child_page_status": 200,
                "verification_mode": "http_api_full",
            }
            with (
                patch.object(
                    verify_httpbridge_ui,
                    "resolve_base_url_candidates",
                    return_value=["https://127.0.0.1:64123", "https://127.0.0.1:4123"],
                ),
                patch.object(verify_httpbridge_ui, "safe_run_probe") as run_probe,
                patch.object(verify_httpbridge_ui, "maybe_mint_local_session_token_if_available", return_value="minted-token") as mint_token,
                patch.object(verify_httpbridge_ui, "run_direct_verification", side_effect=[failed_result, passed_result]) as run_direct,
                patch.object(sys, "argv", [
                    "verify_httpbridge_ui.py",
                    "--runtime-root",
                    str(runtime_root),
                    "--provider",
                    "codex",
                    "--parent-session-id",
                    "parent-123",
                ]),
            ):
                exit_code = verify_httpbridge_ui.main()

        self.assertEqual(exit_code, 0)
        run_probe.assert_not_called()
        mint_token.assert_called_once_with(
            runtime_root,
            username="root",
            active_session_id="parent-123",
        )
        self.assertEqual(run_direct.call_count, 2)
        self.assertEqual(run_direct.call_args_list[0].kwargs["base_url"], "https://127.0.0.1:64123")
        self.assertEqual(run_direct.call_args_list[1].kwargs["base_url"], "https://127.0.0.1:4123")

    def test_main_http_api_mode_does_not_force_auto_resolved_parent_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / ".aize-runtime"
            runtime_root.mkdir(parents=True)
            direct_result = {
                "ok": True,
                "provider": "codex",
                "session_markers": {"session_map": True, "workspace_history": True, "nodes": True, "requests": True, "goal_editor": True},
                "child_markers": {"session_map": True, "workspace_history": True, "nodes": True, "requests": True, "goal_editor": True},
                "created_session_id": "child-direct",
                "goal_update_status": 202,
                "provider_update_status": 200,
                "effective_provider": "codex",
                "prompt_send_status": 202,
                "prompt_provider": "codex",
                "persisted_goal_text": verify_httpbridge_ui.TARGET_GOAL_TEXT,
                "target_goal_text": verify_httpbridge_ui.TARGET_GOAL_TEXT,
                "root_status": 200,
                "session_create_status": 201,
                "child_page_status": 200,
                "verification_mode": "http_api_full",
            }
            with (
                patch.object(verify_httpbridge_ui, "safe_run_probe") as run_probe,
                patch.object(verify_httpbridge_ui, "resolve_probe_parent_session_id", return_value="auto-parent") as resolve_parent,
                patch.object(verify_httpbridge_ui, "maybe_mint_local_session_token_if_available", return_value="minted-token") as mint_token,
                patch.object(verify_httpbridge_ui, "run_direct_verification", return_value=direct_result) as run_direct,
                patch.object(sys, "argv", [
                    "verify_httpbridge_ui.py",
                    "--runtime-root",
                    str(runtime_root),
                    "--provider",
                    "codex",
                    "--verification-mode",
                    "http_api",
                ]),
            ):
                exit_code = verify_httpbridge_ui.main()

        self.assertEqual(exit_code, 0)
        run_probe.assert_not_called()
        resolve_parent.assert_called_once()
        mint_token.assert_called_once_with(runtime_root, username="root", active_session_id="")
        run_direct.assert_called_once()
        self.assertEqual(run_direct.call_args.kwargs["parent_session_id"], "auto-parent")
        self.assertEqual(run_direct.call_args.kwargs["session_token"], "minted-token")

    def test_main_uses_direct_http_verification_by_default_without_session_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / ".aize-runtime"
            runtime_root.mkdir(parents=True)
            direct_result = {
                "ok": True,
                "provider": "codex",
                "session_markers": {"session_map": True, "workspace_history": True, "nodes": True, "requests": True, "goal_editor": True},
                "child_markers": {"session_map": True, "workspace_history": True, "nodes": True, "requests": True, "goal_editor": True},
                "created_session_id": "child-1",
                "goal_update_status": 202,
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
                patch.object(verify_httpbridge_ui, "safe_run_probe") as run_probe,
                patch.object(verify_httpbridge_ui, "maybe_mint_local_session_token_if_available", return_value="minted-token") as mint_token,
                patch.object(
                    verify_httpbridge_ui,
                    "resolve_base_url_candidates",
                    return_value=["https://127.0.0.1:4123"],
                ),
                patch.object(verify_httpbridge_ui, "resolve_probe_parent_session_id", return_value="") as resolve_parent,
                patch.object(verify_httpbridge_ui, "run_direct_verification", return_value=direct_result) as run_direct,
                patch.object(sys, "argv", [
                    "verify_httpbridge_ui.py",
                    "--runtime-root",
                    str(runtime_root),
                    "--provider",
                    "codex",
                    "--verification-mode",
                    "http_api",
                ]),
            ):
                exit_code = verify_httpbridge_ui.main()

        self.assertEqual(exit_code, 0)
        run_probe.assert_not_called()
        mint_token.assert_called_once_with(
            runtime_root,
            username="root",
            active_session_id="",
        )
        resolve_parent.assert_called_once()
        run_direct.assert_called_once()
        self.assertEqual(run_direct.call_args.kwargs["session_token"], "minted-token")

    def test_main_chrome_fallback_retries_direct_http_without_auto_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / ".aize-runtime"
            runtime_root.mkdir(parents=True)
            probe_result = {
                "ok": False,
                "error": "chrome_timeout:13",
                "chrome_bin": "/usr/bin/google-chrome",
            }
            failing_direct_result = {
                "ok": False,
                "provider": "codex",
                "session_markers": {"session_map": False, "workspace_history": False, "nodes": False, "requests": False, "goal_editor": False},
                "child_markers": {"session_map": False, "workspace_history": False, "nodes": False, "requests": False, "goal_editor": False},
                "created_session_id": "",
                "goal_update_status": 0,
                "provider_update_status": 0,
                "effective_provider": "codex",
                "prompt_send_status": 0,
                "prompt_provider": "",
                "persisted_goal_text": "",
                "target_goal_text": verify_httpbridge_ui.TARGET_GOAL_TEXT,
                "root_status": 598,
                "session_create_status": 598,
                "child_page_status": 0,
                "verification_mode": "http_api",
            }
            successful_direct_result = {
                "ok": True,
                "provider": "codex",
                "session_markers": {"session_map": True, "workspace_history": True, "nodes": True, "requests": True, "goal_editor": True},
                "child_markers": {"session_map": True, "workspace_history": True, "nodes": True, "requests": True, "goal_editor": True},
                "created_session_id": "child-retry",
                "goal_update_status": 202,
                "provider_update_status": 200,
                "effective_provider": "codex",
                "prompt_send_status": 202,
                "prompt_provider": "codex",
                "persisted_goal_text": verify_httpbridge_ui.TARGET_GOAL_TEXT,
                "target_goal_text": verify_httpbridge_ui.TARGET_GOAL_TEXT,
                "root_status": 200,
                "session_create_status": 201,
                "child_page_status": 200,
                "verification_mode": "http_api_full",
            }
            with (
                patch.object(verify_httpbridge_ui, "resolve_probe_parent_session_id", return_value="auto-parent") as resolve_parent,
                patch.object(
                    verify_httpbridge_ui,
                    "resolve_base_url_candidates",
                    return_value=["https://127.0.0.1:4123", "https://127.0.0.1:64123"],
                ),
                patch.object(verify_httpbridge_ui, "safe_run_probe", return_value=probe_result) as run_probe,
                patch.object(verify_httpbridge_ui, "maybe_mint_local_session_token_if_available", return_value="minted-token") as mint_token,
                patch.object(
                    verify_httpbridge_ui,
                    "run_direct_verification",
                    side_effect=[failing_direct_result, successful_direct_result],
                ) as run_direct,
                patch.object(sys, "argv", [
                    "verify_httpbridge_ui.py",
                    "--runtime-root",
                    str(runtime_root),
                    "--provider",
                    "codex",
                    "--session-token",
                    "existing-session-token",
                    "--verification-mode",
                    "chrome",
                ]),
            ):
                exit_code = verify_httpbridge_ui.main()

        self.assertEqual(exit_code, 0)
        resolve_parent.assert_called()
        run_probe.assert_called_once()
        mint_token.assert_called_once_with(runtime_root, username="root", active_session_id="")
        self.assertEqual(run_direct.call_count, 2)
        self.assertEqual(run_direct.call_args_list[0].kwargs["session_token"], "minted-token")
        self.assertEqual(run_direct.call_args_list[0].kwargs["parent_session_id"], "auto-parent")
        self.assertEqual(run_direct.call_args_list[1].kwargs["session_token"], "minted-token")
        self.assertEqual(run_direct.call_args_list[1].kwargs["parent_session_id"], "")

    def test_run_direct_verification_keeps_existing_session_token_when_password_auth_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / ".aize-runtime"
            runtime_root.mkdir(parents=True)
            session_root = runtime_root.parent / ".aize-state" / "sessions" / "root" / "child-1"
            session_root.mkdir(parents=True)
            (session_root / "session.json").write_text(
                json.dumps({"goal_text": verify_httpbridge_ui.TARGET_GOAL_TEXT}),
                encoding="utf-8",
            )

            http_calls: list[tuple[str, str]] = []

            def fake_http_request(**kwargs):
                path = kwargs["path"]
                token = kwargs.get("session_token", "")
                http_calls.append((path, token))
                if path == "/":
                    return 598, "", {"error": "http_timeout"}, ""
                if path == "/sessions":
                    if token == "minted-token":
                        return 201, "", {"active_session_id": "child-1", "session": {"session_id": "child-1"}}, ""
                    return 401, "", {"error": "auth_required_or_invalid_session"}, ""
                if path == "/session/select":
                    return 202, "", {}, ""
                if path == "/session/goal":
                    return 202, "", {}, ""
                if path == "/session/goal/state":
                    return 200, "", {"preferred_provider": "codex"}, ""
                if path == "/message":
                    return 202, "", {"provider": "codex"}, ""
                if path.startswith("/?"):
                    return 200, "<div id='session-map-pane'></div><div id='workspace-view'></div><div id='messages'></div>", {}, ""
                self.fail(f"unexpected path {path}")

            with (
                patch.object(verify_httpbridge_ui, "http_request", side_effect=fake_http_request),
                patch.object(verify_httpbridge_ui, "authenticate_http_session", return_value=("", "")),
            ):
                result = verify_httpbridge_ui.run_direct_verification(
                    runtime_root=runtime_root,
                    base_url="https://127.0.0.1:64123",
                    session_token="minted-token",
                    parent_session_id="parent-1",
                    provider="codex",
                    password="unused",
                    username="root",
                )

        self.assertTrue(result["ok"])
        self.assertIn(("/sessions", "minted-token"), http_calls)
        self.assertEqual(result["auth_mode"], "session_token")

    def test_run_direct_verification_refreshes_root_markers_after_local_token_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / ".aize-runtime"
            runtime_root.mkdir(parents=True)
            session_root = runtime_root.parent / ".aize-state" / "sessions" / "root" / "child-1"
            session_root.mkdir(parents=True)
            (session_root / "session.json").write_text(
                json.dumps({"goal_text": verify_httpbridge_ui.TARGET_GOAL_TEXT}),
                encoding="utf-8",
            )

            root_calls: list[str] = []

            def fake_http_request(**kwargs):
                path = kwargs["path"]
                token = kwargs.get("session_token", "")
                if path == "/":
                    root_calls.append(token)
                    if token == "minted-token":
                        return 200, "<div id='session-map-pane'></div><div id='workspace-view'></div><div id='messages'></div>", {}, ""
                    return 200, "<title>HttpBridge Login</title>", {}, ""
                if path == "/sessions":
                    if token == "minted-token":
                        return 201, "", {"active_session_id": "child-1", "session": {"session_id": "child-1"}}, ""
                    return 401, "", {"error": "auth_required_or_invalid_session"}, ""
                if path == "/session/goal":
                    return 202, "", {}, ""
                if path == "/session/goal/state":
                    return 200, "", {"preferred_provider": "codex"}, ""
                if path == "/message":
                    return 202, "", {"provider": "codex"}, ""
                if path.startswith("/?"):
                    return 200, "<div id='session-map-pane'></div><div id='workspace-view'></div><div id='messages'></div>", {}, ""
                self.fail(f"unexpected path {path}")

            with (
                patch.object(verify_httpbridge_ui, "http_request", side_effect=fake_http_request),
                patch.object(verify_httpbridge_ui, "authenticate_http_session", return_value=("", "")),
                patch.object(verify_httpbridge_ui, "maybe_mint_local_session_token_if_available", return_value="minted-token"),
            ):
                result = verify_httpbridge_ui.run_direct_verification(
                    runtime_root=runtime_root,
                    base_url="https://127.0.0.1:64123",
                    session_token="",
                    parent_session_id="",
                    provider="codex",
                    password="unused",
                    username="root",
                )

        self.assertTrue(result["full_page_ok"])
        self.assertEqual(result["verification_mode"], "http_api_full")
        self.assertEqual(root_calls, ["", "minted-token"])

    def test_resolve_probe_parent_session_id_skips_default_session_without_write_permissions(self) -> None:
        sessions = [
            {
                "session_id": "default",
                "label": "Root",
                "created_by_type": "user",
                "updated_at": "2026-05-04T04:00:36Z",
                "session_permissions": {
                    "create_child_session": True,
                    "update_goal": False,
                    "send_prompt": False,
                },
            },
            {
                "session_id": "child-writeable",
                "label": "UI Verify Child",
                "created_by_type": "user",
                "updated_at": "2026-05-04T03:56:47Z",
                "session_permissions": {
                    "create_child_session": True,
                    "update_goal": True,
                    "send_prompt": True,
                },
            },
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / ".aize-runtime"
            runtime_root.mkdir(parents=True)
            with patch.object(verify_httpbridge_ui, "list_sessions", return_value=sessions):
                resolved = verify_httpbridge_ui.resolve_probe_parent_session_id(runtime_root, username="root")

        self.assertEqual(resolved, "child-writeable")

    def test_resolve_probe_parent_session_id_bootstraps_writeable_parent_when_only_default_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / ".aize-runtime"
            runtime_root.mkdir(parents=True)
            with patch.object(verify_httpbridge_ui, "list_sessions", return_value=[]):
                session_id = verify_httpbridge_ui.resolve_probe_parent_session_id(runtime_root, username="root")

            self.assertTrue(session_id)
            created = verify_httpbridge_ui.get_session_settings(
                runtime_root,
                username="root",
                session_id=session_id,
            )

        assert created is not None
        self.assertEqual(created["label"], "UI Verify Parent")
        self.assertTrue(created["session_permissions"]["create_child_session"])
        self.assertTrue(created["session_permissions"]["update_goal"])
        self.assertTrue(created["session_permissions"]["send_prompt"])

    def test_run_direct_verification_accepts_goal_save_only_when_authenticated_pages_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / ".aize-runtime"
            runtime_root.mkdir(parents=True)
            session_root = runtime_root.parent / ".aize-state" / "sessions" / "root" / "child-1"
            session_root.mkdir(parents=True)
            (session_root / "session.json").write_text(
                json.dumps({"goal_text": verify_httpbridge_ui.TARGET_GOAL_TEXT}),
                encoding="utf-8",
            )

            def fake_http_request(**kwargs):
                path = kwargs["path"]
                token = kwargs.get("session_token", "")
                if path == "/":
                    self.assertEqual(token, "minted-token")
                    return 598, "", {"error": "http_timeout"}, ""
                if path == "/sessions":
                    self.assertEqual(token, "minted-token")
                    return 201, "", {"active_session_id": "child-1", "session": {"session_id": "child-1"}}, ""
                if path == "/session/goal":
                    return 202, "", {}, ""
                if path == "/session/goal/state":
                    return 200, "", {"preferred_provider": "codex"}, ""
                if path == "/message":
                    return 202, "", {"provider": "codex"}, ""
                if path.startswith("/?session_id="):
                    return 598, "", {"error": "http_timeout"}, ""
                self.fail(f"unexpected path {path}")

            with (
                patch.object(verify_httpbridge_ui, "http_request", side_effect=fake_http_request),
                patch.object(verify_httpbridge_ui, "authenticate_http_session", return_value=("", "")),
            ):
                result = verify_httpbridge_ui.run_direct_verification(
                    runtime_root=runtime_root,
                    base_url="https://127.0.0.1:64123",
                    session_token="minted-token",
                    parent_session_id="parent-1",
                    provider="codex",
                    password="unused",
                    username="root",
                )

        self.assertTrue(result["ok"])
        self.assertTrue(result["write_flow_ok"])
        self.assertFalse(result["full_page_ok"])
        self.assertEqual(result["verification_mode"], "http_api_goal_save_only")

    def test_run_direct_verification_reauthenticates_when_session_create_rejects_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / ".aize-runtime"
            runtime_root.mkdir(parents=True)
            session_root = runtime_root.parent / ".aize-state" / "sessions" / "root" / "child-1"
            session_root.mkdir(parents=True)
            (session_root / "session.json").write_text(
                json.dumps({"goal_text": verify_httpbridge_ui.TARGET_GOAL_TEXT}),
                encoding="utf-8",
            )

            http_calls: list[tuple[str, str]] = []

            def fake_http_request(**kwargs):
                path = kwargs["path"]
                token = kwargs.get("session_token", "")
                http_calls.append((path, token))
                if path == "/":
                    if token == "bridge-token":
                        return 200, "<div id='session-map-pane'></div><div id='workspace-view'></div><div id='messages'></div>", {}, ""
                    return 202, "", {}, ""
                if path == "/sessions":
                    if token == "minted-token":
                        return 401, "", {"error": "auth_required_or_invalid_talk"}, ""
                    if token == "bridge-token":
                        return 201, "", {"active_session_id": "child-1", "session": {"session_id": "child-1"}}, ""
                    return 401, "", {"error": "auth_required_or_invalid_session"}, ""
                if path == "/session/select":
                    return 202, "", {}, ""
                if path == "/session/goal":
                    return 200, "", {}, ""
                if path == "/session/goal/state":
                    return 200, "", {"preferred_provider": "codex"}, ""
                if path == "/message":
                    return 202, "", {"provider": "codex"}, ""
                if path.startswith("/?"):
                    return 200, "<div id='session-map-pane'></div><div id='workspace-view'></div><div id='messages'></div>", {}, ""
                self.fail(f"unexpected path {path}")

            with (
                patch.object(verify_httpbridge_ui, "http_request", side_effect=fake_http_request),
                patch.object(verify_httpbridge_ui, "authenticate_http_session", return_value=("bridge-token", "login")) as auth_session,
            ):
                result = verify_httpbridge_ui.run_direct_verification(
                    runtime_root=runtime_root,
                    base_url="https://127.0.0.1:64123",
                    session_token="minted-token",
                    parent_session_id="parent-1",
                    provider="codex",
                    password="unused",
                    username="root",
                )

        self.assertTrue(result["ok"])
        self.assertEqual(auth_session.call_count, 1)
        self.assertEqual(result["auth_mode"], "session_token")
        self.assertIn(("/", "minted-token"), http_calls)
        self.assertIn(("/", "bridge-token"), http_calls)
        self.assertIn(("/sessions", "bridge-token"), http_calls)

    def test_run_direct_verification_remints_local_session_token_when_reauth_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / ".aize-runtime"
            runtime_root.mkdir(parents=True)
            session_root = runtime_root.parent / ".aize-state" / "sessions" / "root" / "child-1"
            session_root.mkdir(parents=True)
            (session_root / "session.json").write_text(
                json.dumps({"goal_text": verify_httpbridge_ui.TARGET_GOAL_TEXT}),
                encoding="utf-8",
            )

            http_calls: list[tuple[str, str]] = []

            def fake_http_request(**kwargs):
                path = kwargs["path"]
                token = kwargs.get("session_token", "")
                http_calls.append((path, token))
                if path == "/":
                    if token == "fresh-token":
                        return 200, "<div id='session-map-pane'></div><div id='workspace-view'></div><div id='messages'></div>", {}, ""
                    return 200, "", {}, ""
                if path == "/sessions":
                    if token == "stale-token":
                        return 401, "", {"error": "auth_required_or_invalid_talk"}, ""
                    if token == "fresh-token":
                        return 201, "", {"active_session_id": "child-1", "session": {"session_id": "child-1"}}, ""
                    return 401, "", {"error": "auth_required_or_invalid_session"}, ""
                if path == "/session/select":
                    return 200, "", {}, ""
                if path == "/session/goal":
                    return 200, "", {}, ""
                if path == "/session/goal/state":
                    return 200, "", {"preferred_provider": "codex"}, ""
                if path == "/message":
                    return 202, "", {"provider": "codex"}, ""
                if path.startswith("/?"):
                    return 200, "<div id='session-map-pane'></div><div id='workspace-view'></div><div id='messages'></div>", {}, ""
                self.fail(f"unexpected path {path}")

            with (
                patch.object(verify_httpbridge_ui, "http_request", side_effect=fake_http_request),
                patch.object(verify_httpbridge_ui, "authenticate_http_session", return_value=("", "")) as auth_session,
                patch.object(verify_httpbridge_ui, "maybe_mint_local_session_token_if_available", return_value="fresh-token") as mint_token,
            ):
                result = verify_httpbridge_ui.run_direct_verification(
                    runtime_root=runtime_root,
                    base_url="https://127.0.0.1:4123",
                    session_token="stale-token",
                    parent_session_id="parent-1",
                    provider="codex",
                    password="unused",
                    username="root",
                )

        self.assertTrue(result["ok"])
        self.assertEqual(auth_session.call_count, 1)
        mint_token.assert_called_once_with(
            runtime_root,
            username="root",
            active_session_id="",
        )
        self.assertEqual(result["auth_mode"], "session_token")
        self.assertIn(("/sessions", "fresh-token"), http_calls)

    def test_main_mints_local_session_before_using_direct_http_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / ".aize-runtime"
            runtime_root.mkdir(parents=True)
            timeout_probe_result = {
                "ok": False,
                "error": "chrome_timeout:13",
                "chrome_bin": "/usr/bin/google-chrome",
            }
            token_probe_result = {
                "ok": False,
                "error": "chrome_timeout:13",
                "chrome_bin": "/usr/bin/google-chrome",
            }
            direct_result = {
                "ok": True,
                "provider": "codex",
                "session_markers": {"session_map": True, "workspace_history": True, "nodes": True, "requests": True, "goal_editor": True},
                "child_markers": {"session_map": True, "workspace_history": True, "nodes": True, "requests": True, "goal_editor": True},
                "created_session_id": "child-1",
                "goal_update_status": 202,
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
                patch.object(verify_httpbridge_ui, "safe_run_probe") as run_probe,
                patch.object(verify_httpbridge_ui, "maybe_mint_local_session_token_if_available", return_value="minted-token") as mint_token,
                patch.object(verify_httpbridge_ui, "run_direct_verification", return_value=direct_result) as run_direct,
                patch.object(verify_httpbridge_ui, "resolve_probe_parent_session_id", return_value="parent-auto"),
                patch.object(verify_httpbridge_ui, "resolve_base_url_candidates", return_value=["https://127.0.0.1:4123"]),
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
        run_probe.assert_not_called()
        mint_token.assert_called_once_with(runtime_root, username="root", active_session_id="")
        run_direct.assert_called_once()
        self.assertEqual(run_direct.call_args.kwargs["session_token"], "minted-token")


if __name__ == "__main__":
    unittest.main()
