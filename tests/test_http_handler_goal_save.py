from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from runtime.http_handler import make_handler  # noqa: E402
from runtime.persistent_state_pkg import (  # noqa: E402
    append_history,
    create_conversation_session,
    ensure_state,
    get_history,
    get_session_settings,
    load_pending_inputs,
    load_service_pending_inputs,
    list_sessions,
    session_message_dir,
    session_goal_manager_state_path,
    update_session_goal,
    update_session_goal_flags,
    update_session_skills,
    write_json_file,
)
from runtime.persistent_state_pkg._core import session_metadata_path  # noqa: E402
from runtime.ui_history import build_session_ui_history  # noqa: E402
from unit_file import update_registered_unit_state  # noqa: E402


class _ImmediateThread:
    def __init__(self, *, target, daemon=None, args=(), kwargs=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}
        self.daemon = daemon

    def start(self) -> None:
        self._target(*self._args, **self._kwargs)


class HttpHandlerGoalSaveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.runtime_root = Path(self.tempdir.name) / "runtime"
        ensure_state(self.runtime_root)
        (self.runtime_root / "logs").mkdir(parents=True, exist_ok=True)
        talk = create_conversation_session(self.runtime_root, username="root", label="HTTP Goal Save")
        self.session_id = str(talk["session_id"])

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _make_handler(self, enqueue_goal_dispatch, *, current_context=None, requested_session_id=None):
        return make_handler(
            runtime_root=self.runtime_root,
            manifest={},
            self_service={"service_id": "service-http-001", "display_name": "AIze"},
            process_id="proc-http-001",
            log_path=self.runtime_root / "logs" / "http.jsonl",
            default_target="service-codex-001",
            default_provider="codex",
            history_limit=100,
            tls_enabled=True,
            codex_service_pool=[],
            claude_service_pool=[],
            gemini_service_pool=[],
            llm_service_kinds={},
            pending=[],
            awaiting_replies={},
            subscribers={},
            subscribers_lock=threading.Lock(),
            stopped=threading.Event(),
            _active_goal_audits={},
            _active_goal_audits_lock=threading.Lock(),
            _active_agent_turns={},
            _active_agent_turns_lock=threading.Lock(),
            release_stale_session_bindings=lambda: None,
            subscriber_key=lambda *args, **kwargs: "",
            append_history=lambda username, session_id, entry: append_history(
                self.runtime_root,
                username=username,
                session_id=session_id,
                entry=entry,
                limit=100,
            ),
            send_router_control=lambda *args, **kwargs: None,
            enqueue_service_control=lambda *args, **kwargs: None,
            service_snapshots=lambda: {},
            session_runtime_payload=lambda *args, **kwargs: {},
            peer_descriptor=lambda *args, **kwargs: {},
            resolve_session_service_for_dispatch=lambda *args, **kwargs: "",
            codex_service_candidates_for_session=lambda *args, **kwargs: [],
            current_llm_service_topology=lambda: ([], [], [], {}),
            resolve_bound_codex_session=lambda *args, **kwargs: "",
            enqueue_goal_dispatch=enqueue_goal_dispatch,
            session_auto_compact_threshold=lambda *args, **kwargs: 20,
            context_status_from_entry=lambda *args, **kwargs: {},
            latest_context_status=lambda *args, **kwargs: {},
            stored_context_status=lambda *args, **kwargs: {},
            refresh_context_status=lambda *args, **kwargs: {},
            ensure_context_status=lambda *args, **kwargs: {},
            manual_compact_current_session=lambda *args, **kwargs: None,
            render_entry_html=lambda *args, **kwargs: "",
            cookie_value=lambda *args, **kwargs: "",
            request_parts=lambda *args, **kwargs: ("", {}),
            requested_session_id=requested_session_id or (lambda *args, **kwargs: ""),
            request_positive_int=lambda *args, **kwargs: 0,
            current_context=current_context or (lambda *args, **kwargs: {}),
        )

    @staticmethod
    def _multipart_payload(*, fields: dict[str, str], files: list[tuple[str, str, bytes, str]], boundary: str) -> bytes:
        chunks: list[bytes] = []
        for key, value in fields.items():
            chunks.extend(
                [
                    f"--{boundary}\r\n".encode(),
                    f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(),
                    value.encode("utf-8"),
                    b"\r\n",
                ]
            )
        for field_name, filename, data, content_type in files:
            chunks.extend(
                [
                    f"--{boundary}\r\n".encode(),
                    (
                        f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
                        f"Content-Type: {content_type}\r\n\r\n"
                    ).encode(),
                    data,
                    b"\r\n",
                ]
            )
        chunks.append(f"--{boundary}--\r\n".encode())
        return b"".join(chunks)

    def test_root_page_renders_session_map_with_registered_unit_metadata(self) -> None:
        update_session_goal(
            self.runtime_root,
            username="root",
            session_id=self.session_id,
            goal_text="Keep the root UI rendering.",
        )
        update_session_goal_flags(
            self.runtime_root,
            username="root",
            session_id=self.session_id,
            goal_active=True,
        )

        Handler = self._make_handler(
            lambda **kwargs: ("", ""),
            current_context=lambda *args, **kwargs: {
                "username": "root",
                "viewer_username": "root",
                "session_id": self.session_id,
                "roles": ["root", "superuser"],
                "role": "root",
                "is_superuser": True,
            },
            requested_session_id=lambda *args, **kwargs: None,
        )
        handler = object.__new__(Handler)
        responses: list[tuple[int, str]] = []
        handler._trace_auth_request = lambda *args, **kwargs: None
        handler._html = lambda status, body: responses.append((status, body))

        handler._do_GET_root("/", {})

        self.assertEqual(responses[0][0], 200)
        self.assertIn("session-map-pane", responses[0][1])
        self.assertIn("goal-board-grid", responses[0][1])
        self.assertIn(self.session_id, responses[0][1])
        self.assertIn("workspace-nav-status", responses[0][1])
        self.assertIn("<span class='workspace-nav-chip is-active'>Goal Active</span>", responses[0][1])
        self.assertIn("<span class='workspace-nav-chip'>Goal In Progress</span>", responses[0][1])
        self.assertIn("<span class='workspace-nav-chip'>Runtime Idle</span>", responses[0][1])
        self.assertIn("<span class='workspace-nav-chip'>All Clear</span>", responses[0][1])
        self.assertIn("<span class='goal-session-badge is-on'>Goal Active</span>", responses[0][1])
        self.assertIn("<span class='goal-session-badge'>Goal In Progress</span>", responses[0][1])
        self.assertIn("<span class='goal-session-badge'>Runtime Idle</span>", responses[0][1])
        self.assertIn("<span class='goal-session-badge is-audit-ok'>All Clear</span>", responses[0][1])

        json_responses: list[tuple[int, dict]] = []
        handler._json = lambda status, payload: json_responses.append((status, payload))
        handler._do_GET_sessions("/sessions", {"_": ["1"]})

        self.assertEqual(json_responses[-1][0], 200)
        summary = next(
            item
            for item in json_responses[-1][1]["session_summaries"]
            if item["session_id"] == self.session_id
        )
        self.assertEqual(summary["goal_audit_state"], "all_clear")
        self.assertEqual(summary["runtime_execution_state"], "idle")

    def test_message_goal_mode_resets_goal_manager_runtime_state(self) -> None:
        state_path = session_goal_manager_state_path(
            self.runtime_root,
            username="root",
            session_id=self.session_id,
        )
        write_json_file(
            state_path,
            {
                "state": "running",
                "goal_audit_job_id": "goal-audit-stale",
                "service_id": "service-claude-001",
                "pending_work_items": [{"kind": "turn_completed"}],
            },
        )

        dispatch_calls: list[dict[str, str]] = []

        def enqueue_goal_dispatch(**kwargs):
            dispatch_calls.append(kwargs)
            return "service-codex-001", ""

        Handler = self._make_handler(enqueue_goal_dispatch)
        handler = object.__new__(Handler)
        responses: list[tuple[int, dict]] = []
        handler._require_user = lambda payload=None: {"username": "root", "session_id": self.session_id}
        handler._json = lambda status, payload: responses.append((status, payload))

        with (
            patch("runtime.http_handler.issue_auth_context", return_value={"username": "root"}),
            patch("runtime.http_handler.threading.Thread", _ImmediateThread),
        ):
            handler._do_POST_message(
                {"mode": "goal", "text": "Verify HTTPBridge goal save flow updated"},
                "application/json",
            )

        self.assertEqual(responses[0][0], 202)
        self.assertEqual(responses[0][1]["mode"], "goal")
        self.assertEqual(len(dispatch_calls), 1)

        talk = get_session_settings(self.runtime_root, username="root", session_id=self.session_id)
        assert talk is not None
        self.assertEqual(talk["goal_text"], "Verify HTTPBridge goal save flow updated")

        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["state"], "idle")
        self.assertEqual(state["goal_audit_job_id"], "")
        self.assertEqual(state["service_id"], "")
        self.assertEqual(state["pending_work_items"], [])

        history = get_history(self.runtime_root, username="root", session_id=self.session_id)
        self.assertEqual(history[-1]["event_type"], "service.goal_manager_reset")
        self.assertEqual(history[-1]["event"]["reason"], "goal_updated")

    def test_message_multipart_upload_persists_message_directory_and_attachment_metadata(self) -> None:
        Handler = self._make_handler(lambda **kwargs: ("", ""))
        handler = object.__new__(Handler)
        responses: list[tuple[int, dict]] = []
        handler._require_user = lambda payload=None: {"username": "root", "session_id": self.session_id}
        handler._json = lambda status, payload: responses.append((status, payload))
        handler._redirect = lambda location: responses.append((303, {"location": location}))
        boundary = "----aize-boundary"
        body = self._multipart_payload(
            fields={"text": "Please inspect this file", "mode": "prompt"},
            files=[("file", "note.txt", b"hello attachment", "text/plain")],
            boundary=boundary,
        )

        with (
            patch("runtime.http_handler.issue_auth_context", return_value={"username": "root"}),
            patch("runtime.http_handler.threading.Thread", _ImmediateThread),
        ):
            handler._do_POST_message_multipart(body, f"multipart/form-data; boundary={boundary}")

        self.assertEqual(responses[0][0], 303)
        pending = load_pending_inputs(self.runtime_root, username="root", session_id=self.session_id)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["kind"], "user_message")
        self.assertIn("[Attached files]", pending[0]["text"])
        self.assertTrue(pending[0]["message_id"])
        self.assertEqual(len(pending[0]["attachments"]), 1)
        message_path = session_message_dir(
            self.runtime_root,
            username="root",
            session_id=self.session_id,
            message_id=pending[0]["message_id"],
        )
        self.assertTrue((message_path / "body.txt").exists())
        self.assertTrue((message_path / "meta.json").exists())
        self.assertTrue((message_path / "attachments" / "note.txt").exists())

    def test_message_prompt_runs_interactive_session_skill_through_history_path(self) -> None:
        talk = create_conversation_session(
            self.runtime_root,
            username="root",
            label="Interactive Skill",
            communication_agent_enabled=True,
        )
        session_id = str(talk["session_id"])
        update_session_skills(
            self.runtime_root,
            username="root",
            session_id=session_id,
            session_skills=[
                {
                    "skill_id": "http-echo",
                    "kind": "interactive",
                    "routing_mode": "handle_user_message",
                    "routing_tags": ["echo"],
                    "handler_file": "skill.py",
                    "files": [
                        {
                            "path": "skill.py",
                            "content": (
                                "def handle(context):\n"
                                "    return {'assistant_text': 'http skill: ' + context['prompt_text']}\n"
                            ),
                        }
                    ],
                }
            ],
        )
        router_calls: list[dict] = []
        Handler = make_handler(
            runtime_root=self.runtime_root,
            manifest={"node_id": "node-test", "run_id": "run-test"},
            self_service={"service_id": "service-http-001"},
            process_id="proc-http-001",
            log_path=self.runtime_root / "logs" / "http.jsonl",
            default_target="service-codex-001",
            default_provider="codex",
            history_limit=100,
            tls_enabled=True,
            codex_service_pool=[],
            claude_service_pool=[],
            gemini_service_pool=[],
            llm_service_kinds={},
            pending=[],
            awaiting_replies={},
            subscribers={},
            subscribers_lock=threading.Lock(),
            stopped=threading.Event(),
            _active_goal_audits={},
            _active_goal_audits_lock=threading.Lock(),
            _active_agent_turns={},
            _active_agent_turns_lock=threading.Lock(),
            release_stale_session_bindings=lambda: None,
            subscriber_key=lambda *args, **kwargs: "",
            append_history=lambda username, sid, entry: append_history(
                self.runtime_root,
                username=username,
                session_id=sid,
                entry=entry,
                limit=100,
            ),
            send_router_control=lambda message: router_calls.append(message) or True,
            enqueue_service_control=lambda *args, **kwargs: None,
            service_snapshots=lambda: {},
            session_runtime_payload=lambda *args, **kwargs: {},
            peer_descriptor=lambda *args, **kwargs: {},
            resolve_session_service_for_dispatch=lambda *args, **kwargs: "",
            codex_service_candidates_for_session=lambda *args, **kwargs: [],
            current_llm_service_topology=lambda: ([], [], [], {}),
            resolve_bound_codex_session=lambda *args, **kwargs: "",
            enqueue_goal_dispatch=lambda **kwargs: ("", ""),
            session_auto_compact_threshold=lambda *args, **kwargs: 20,
            context_status_from_entry=lambda *args, **kwargs: {},
            latest_context_status=lambda *args, **kwargs: {},
            stored_context_status=lambda *args, **kwargs: {},
            refresh_context_status=lambda *args, **kwargs: {},
            ensure_context_status=lambda *args, **kwargs: {},
            manual_compact_current_session=lambda *args, **kwargs: None,
            render_entry_html=lambda *args, **kwargs: "",
            cookie_value=lambda *args, **kwargs: "",
            request_parts=lambda *args, **kwargs: ("", {}),
            requested_session_id=lambda *args, **kwargs: "",
            request_positive_int=lambda *args, **kwargs: 0,
            current_context=lambda *args, **kwargs: {},
        )
        handler = object.__new__(Handler)
        responses: list[tuple[int, dict]] = []
        handler._require_user = lambda payload=None: {"username": "root", "session_id": session_id}
        handler._json = lambda status, payload: responses.append((status, payload))

        with (
            patch("runtime.http_handler.issue_auth_context", return_value={"username": "root"}),
            patch("runtime.http_handler.threading.Thread", _ImmediateThread),
        ):
            handler._do_POST_message(
                {"mode": "prompt", "text": "echo through http skill", "session_id": session_id},
                "application/json",
            )

        self.assertEqual(responses[0][0], 202)
        history = get_history(self.runtime_root, username="root", session_id=session_id)
        self.assertTrue(any(entry.get("direction") == "out" for entry in history))
        self.assertTrue(any(entry.get("event_type") == "agent.turn_started" for entry in history))
        self.assertTrue(any(entry.get("direction") == "in" and entry.get("text") == "http skill: echo through http skill" for entry in history))
        self.assertTrue(any(entry.get("event_type") == "turn.completed" for entry in history))
        ui_history = build_session_ui_history(
            self.runtime_root,
            username="root",
            session_id=session_id,
            limit=20,
        )
        self.assertTrue(any(entry.get("direction") == "in" and entry.get("text") == "http skill: echo through http skill" for entry in ui_history))
        self.assertEqual(router_calls, [])

    def test_message_prompt_routes_entrance_request_through_development_child_proxy_path(self) -> None:
        talk = create_conversation_session(
            self.runtime_root,
            username="root",
            label="Entrance",
            session_ui_mode="communication",
            communication_agent_enabled=True,
            session_skills=[
                {
                    "skill_id": "canonical-development-routing",
                    "routing_mode": "create_child_session",
                        "route_when_unhandled": True,
                    "routing_tags": ["development", "implement", "fix"],
                    "canonical_session_key": "aize.development",
                    "target_template_id": "aize-development.bug-hunting",
                    "target_label": "AIze Development",
                    "target_goal_text": "Coordinate routed development work.",
                    "preferred_provider": "codex",
                    "selected_agents": ["codex_pool"],
                }
            ],
        )
        session_id = str(talk["session_id"])
        updated = update_session_goal(
            self.runtime_root,
            username="root",
            session_id=session_id,
            goal_text="Entrance routes implementation work.",
        )
        self.assertIsNotNone(updated)
        updated = update_session_goal_flags(
            self.runtime_root,
            username="root",
            session_id=session_id,
            goal_active=True,
            goal_completed=True,
            goal_progress_state="complete",
        )
        self.assertIsNotNone(updated)

        router_calls: list[dict[str, object]] = []
        service_kinds = {
            "service-codex-001": "codex",
            "service-codex-002": "codex",
            "service-codex-003": "codex",
            "service-codex-004": "codex",
        }
        Handler = make_handler(
            runtime_root=self.runtime_root,
            manifest={"node_id": "node-test", "run_id": "run-test"},
            self_service={"service_id": "service-http-001"},
            process_id="proc-http-001",
            log_path=self.runtime_root / "logs" / "http.jsonl",
            default_target="service-codex-001",
            default_provider="codex",
            history_limit=100,
            tls_enabled=True,
            codex_service_pool=["service-codex-001", "service-codex-002"],
            claude_service_pool=[],
            gemini_service_pool=[],
            llm_service_kinds=service_kinds,
            pending=[],
            awaiting_replies={},
            subscribers={},
            subscribers_lock=threading.Lock(),
            stopped=threading.Event(),
            _active_goal_audits={},
            _active_goal_audits_lock=threading.Lock(),
            _active_agent_turns={},
            _active_agent_turns_lock=threading.Lock(),
            release_stale_session_bindings=lambda: None,
            subscriber_key=lambda *args, **kwargs: "",
            append_history=lambda username, sid, entry: append_history(
                self.runtime_root,
                username=username,
                session_id=sid,
                entry=entry,
                limit=100,
            ),
            send_router_control=lambda message: router_calls.append(message) or True,
            enqueue_service_control=lambda *args, **kwargs: None,
            service_snapshots=lambda: {},
            session_runtime_payload=lambda *args, **kwargs: {},
            peer_descriptor=lambda *args, **kwargs: {},
            resolve_session_service_for_dispatch=lambda *args, **kwargs: "",
            codex_service_candidates_for_session=lambda *args, **kwargs: [],
            current_llm_service_topology=lambda: (
                ["service-codex-001", "service-codex-002"],
                [],
                [],
                service_kinds,
            ),
            resolve_bound_codex_session=lambda *args, **kwargs: "",
            enqueue_goal_dispatch=lambda **kwargs: ("", ""),
            session_auto_compact_threshold=lambda *args, **kwargs: 20,
            context_status_from_entry=lambda *args, **kwargs: {},
            latest_context_status=lambda *args, **kwargs: {},
            stored_context_status=lambda *args, **kwargs: {},
            refresh_context_status=lambda *args, **kwargs: {},
            ensure_context_status=lambda *args, **kwargs: {},
            manual_compact_current_session=lambda *args, **kwargs: None,
            render_entry_html=lambda *args, **kwargs: "",
            cookie_value=lambda *args, **kwargs: "",
            request_parts=lambda *args, **kwargs: ("", {}),
            requested_session_id=lambda *args, **kwargs: "",
            request_positive_int=lambda *args, **kwargs: 0,
            current_context=lambda *args, **kwargs: {},
        )
        handler = object.__new__(Handler)
        responses: list[tuple[int, dict]] = []
        handler._require_user = lambda payload=None: {"username": "root", "session_id": session_id}
        handler._json = lambda status, payload: responses.append((status, payload))

        with (
            patch("runtime.http_handler.issue_auth_context", return_value={"username": "root"}),
            patch("runtime.http_handler.threading.Thread", _ImmediateThread),
            patch(
                "runtime.http_handler._resolve_dispatch_service_for_session",
                return_value="service-codex-004",
            ),
            patch(
                "runtime.http_handler._resolve_goal_manager_dispatch_service_for_session",
                return_value="service-codex-003",
            ),
        ):
            handler._do_POST_message(
                {
                    "mode": "prompt",
                    "text": "Please implement the development routing fix.",
                    "session_id": session_id,
                },
                "application/json",
            )

        self.assertEqual(responses[0][0], 202)

        sessions = list_sessions(self.runtime_root, username="root")
        created_sessions = [item for item in sessions if str(item.get("origin_session_id") or "") == session_id]
        parent = next(item for item in created_sessions if str(item.get("label") or "") == "AIze Development")
        child = next(item for item in created_sessions if str(item.get("parent_session_id") or "") == str(parent["session_id"]))

        self.assertEqual(parent["launcher_template_id"], "aize-development.bug-hunting")
        self.assertEqual(parent["parent_session_id"], "default")
        self.assertEqual(parent["session_group"], "root")
        self.assertEqual(child["goal_text"], "Please implement the development routing fix.")

        child_pending = load_pending_inputs(
            self.runtime_root,
            username="root",
            session_id=str(child["session_id"]),
        )
        self.assertEqual(len(child_pending), 1)
        self.assertEqual(child_pending[0]["kind"], "goal_feedback")
        self.assertIn("Please implement the development routing fix.", child_pending[0]["text"])

        interactive_pending = load_service_pending_inputs(
            self.runtime_root,
            service_id="service-codex-001",
            agent_id=f"service-codex-001@@{session_id}@@interactive_agent",
            username="root",
            session_id=session_id,
        )
        worker_pending = load_service_pending_inputs(
            self.runtime_root,
            service_id="service-codex-002",
            agent_id=f"service-codex-002@@{session_id}@@worker_agent",
            username="root",
            session_id=session_id,
        )
        self.assertEqual([item["kind"] for item in interactive_pending], ["user_dialogue"])
        self.assertEqual([item["kind"] for item in worker_pending], ["interactive_worker_request"])
        self.assertEqual(worker_pending[0]["delegated_session_id"], child["session_id"])
        self.assertIn("delegated_session", worker_pending[0]["text"])
        self.assertIn(str(child["session_id"]), worker_pending[0]["text"])

        dispatch_reasons = [call.get("payload", {}).get("reason") for call in router_calls]
        self.assertEqual(
            dispatch_reasons,
            ["http_user_dialogue", "interactive_worker_request", "goal_manager_review", "goal_feedback"],
        )

        history = get_history(self.runtime_root, username="root", session_id=session_id)
        last_out = next(entry for entry in reversed(history) if entry.get("direction") == "out")
        self.assertEqual(last_out["to"], f"forward:{child['session_id']}")
        immediate_ack = next(
            entry
            for entry in history
            if entry.get("direction") == "in" and entry.get("service_id") == "service-entrance-router"
        )
        expected_ack = (
            f"Routed to {parent['label']}. Entrance will keep this session updated while that work runs."
        )
        self.assertEqual(immediate_ack["text"], expected_ack)

        ui_history = build_session_ui_history(
            self.runtime_root,
            username="root",
            session_id=session_id,
            limit=20,
        )
        self.assertTrue(
            any(
                entry.get("direction") == "in"
                and entry.get("text") == expected_ack
                for entry in ui_history
            )
        )

        reopened = get_session_settings(self.runtime_root, username="root", session_id=session_id)
        self.assertIsNotNone(reopened)
        assert reopened is not None
        self.assertFalse(bool(reopened.get("goal_completed", False)))
        self.assertEqual(reopened["goal_progress_state"], "in_progress")

    def test_message_prompt_runs_entrance_handler_before_unhandled_development_route(self) -> None:
        talk = create_conversation_session(
            self.runtime_root,
            username="root",
            label="Entrance",
            session_ui_mode="communication",
            communication_agent_enabled=True,
        )
        session_id = str(talk["session_id"])
        update_session_skills(
            self.runtime_root,
            username="root",
            session_id=session_id,
            session_skills=[
                {
                    "skill_id": "entrance-lightweight-response",
                    "kind": "interactive",
                    "routing_mode": "handle_user_message",
                    "handler_file": "entrance.py",
                    "files": [
                        {
                            "path": "entrance.py",
                            "content": (
                                "def handle(context):\n"
                                "    if context['prompt_text'].strip().lower() == 'status':\n"
                                "        return {'handled': True, 'assistant_text': 'status handled'}\n"
                                "    return {'handled': False}\n"
                            ),
                        }
                    ],
                },
                {
                    "skill_id": "canonical-development-routing",
                    "kind": "routing",
                    "routing_mode": "create_child_session",
                    "route_when_unhandled": True,
                    "canonical_session_key": "aize.development",
                    "target_template_id": "aize-development.bug-hunting",
                    "target_label": "AIze Development",
                    "target_goal_text": "Coordinate routed development work.",
                },
            ],
        )

        router_calls: list[dict[str, object]] = []
        Handler = make_handler(
            runtime_root=self.runtime_root,
            manifest={"node_id": "node-test", "run_id": "run-test"},
            self_service={"service_id": "service-http-001"},
            process_id="proc-http-001",
            log_path=self.runtime_root / "logs" / "http.jsonl",
            default_target="service-codex-001",
            default_provider="codex",
            history_limit=100,
            tls_enabled=True,
            codex_service_pool=["service-codex-001", "service-codex-002"],
            claude_service_pool=[],
            gemini_service_pool=[],
            llm_service_kinds={"service-codex-001": "codex", "service-codex-002": "codex"},
            pending=[],
            awaiting_replies={},
            subscribers={},
            subscribers_lock=threading.Lock(),
            stopped=threading.Event(),
            _active_goal_audits={},
            _active_goal_audits_lock=threading.Lock(),
            _active_agent_turns={},
            _active_agent_turns_lock=threading.Lock(),
            release_stale_session_bindings=lambda: None,
            subscriber_key=lambda *args, **kwargs: "",
            append_history=lambda username, sid, entry: append_history(
                self.runtime_root,
                username=username,
                session_id=sid,
                entry=entry,
                limit=100,
            ),
            send_router_control=lambda message: router_calls.append(message) or True,
            enqueue_service_control=lambda *args, **kwargs: None,
            service_snapshots=lambda: {},
            session_runtime_payload=lambda *args, **kwargs: {},
            peer_descriptor=lambda *args, **kwargs: {},
            resolve_session_service_for_dispatch=lambda *args, **kwargs: "",
            codex_service_candidates_for_session=lambda *args, **kwargs: [],
            current_llm_service_topology=lambda: (
                ["service-codex-001", "service-codex-002"],
                [],
                [],
                {"service-codex-001": "codex", "service-codex-002": "codex"},
            ),
            resolve_bound_codex_session=lambda *args, **kwargs: "",
            enqueue_goal_dispatch=lambda **kwargs: ("", ""),
            session_auto_compact_threshold=lambda *args, **kwargs: 20,
            context_status_from_entry=lambda *args, **kwargs: {},
            latest_context_status=lambda *args, **kwargs: {},
            stored_context_status=lambda *args, **kwargs: {},
            refresh_context_status=lambda *args, **kwargs: {},
            ensure_context_status=lambda *args, **kwargs: {},
            manual_compact_current_session=lambda *args, **kwargs: None,
            render_entry_html=lambda *args, **kwargs: "",
            cookie_value=lambda *args, **kwargs: "",
            request_parts=lambda *args, **kwargs: ("", {}),
            requested_session_id=lambda *args, **kwargs: "",
            request_positive_int=lambda *args, **kwargs: 0,
            current_context=lambda *args, **kwargs: {},
        )
        handler = object.__new__(Handler)
        responses: list[tuple[int, dict]] = []
        handler._require_user = lambda payload=None: {"username": "root", "session_id": session_id}
        handler._json = lambda status, payload: responses.append((status, payload))

        with (
            patch("runtime.http_handler.issue_auth_context", return_value={"username": "root"}),
            patch("runtime.http_handler.threading.Thread", _ImmediateThread),
        ):
            handler._do_POST_message(
                {"mode": "prompt", "text": "status", "session_id": session_id},
                "application/json",
            )

        self.assertEqual(responses[0][0], 202)
        history = get_history(self.runtime_root, username="root", session_id=session_id)
        self.assertTrue(any(entry.get("text") == "status handled" for entry in history))
        created_sessions = [
            item
            for item in list_sessions(self.runtime_root, username="root")
            if str(item.get("origin_session_id") or "") == session_id
        ]
        self.assertEqual(created_sessions, [])
        self.assertEqual(router_calls, [])

    def test_get_overview_bypasses_ttl_cache_for_cache_busted_requests(self) -> None:
        Handler = self._make_handler(lambda **kwargs: ("", ""))
        handler = object.__new__(Handler)
        handler._require_user = lambda query=None: {"username": "root", "session_id": self.session_id}

        responses: list[tuple[int, dict]] = []
        handler._json = lambda status, payload: responses.append((status, payload))

        updated = update_session_goal(
            self.runtime_root,
            username="root",
            session_id=self.session_id,
            goal_text="Keep this goal running",
        )
        self.assertIsNotNone(updated)

        handler._do_GET_overview("/overview", {})
        self.assertEqual(responses[-1][0], 200)
        self.assertFalse(
            responses[-1][1]["session_summaries"][0]["goal_completed"],
        )

        updated = update_session_goal_flags(
            self.runtime_root,
            username="root",
            session_id=self.session_id,
            goal_completed=True,
        )
        self.assertIsNotNone(updated)

        handler._do_GET_overview("/overview", {})
        self.assertFalse(
            responses[-1][1]["session_summaries"][0]["goal_completed"],
        )

        handler._do_GET_overview("/overview", {"_": ["1"]})
        self.assertTrue(
            responses[-1][1]["session_summaries"][0]["goal_completed"],
        )

    def test_get_sessions_prefilters_to_recent_and_resident_sessions(self) -> None:
        recent = create_conversation_session(self.runtime_root, username="root", label="Recent Session")
        old = create_conversation_session(self.runtime_root, username="root", label="Old Session")
        resident = create_conversation_session(self.runtime_root, username="root", label="Resident Session")
        old_id = str(old["session_id"])
        resident_id = str(resident["session_id"])

        old_record = get_session_settings(self.runtime_root, username="root", session_id=old_id)
        assert old_record is not None
        old_record["created_at"] = "2026-01-01T00:00:00Z"
        old_record["updated_at"] = "2026-01-01T00:00:00Z"
        write_json_file(
            session_metadata_path(self.runtime_root, username="root", session_id=old_id),
            old_record,
        )

        update_registered_unit_state(
            self.runtime_root,
            username="root",
            unit_id="resident.unit",
            updates={
                "display_name": "Resident Unit",
                "last_session_id": resident_id,
                "last_parent_session_id": self.session_id,
            },
        )

        Handler = self._make_handler(lambda **kwargs: ("", ""))
        handler = object.__new__(Handler)
        handler._require_user = lambda query=None: {
            "username": "root",
            "viewer_username": "root",
            "session_id": self.session_id,
        }
        responses: list[tuple[int, dict]] = []
        handler._json = lambda status, payload: responses.append((status, payload))

        handler._do_GET_sessions("/sessions", {"session_window_seconds": ["86400"]})

        self.assertEqual(responses[-1][0], 200)
        payload = responses[-1][1]
        session_ids = {entry["session_id"] for entry in payload["session_summaries"]}
        self.assertIn(self.session_id, session_ids)
        self.assertIn(str(recent["session_id"]), session_ids)
        self.assertIn(resident_id, session_ids)
        self.assertNotIn(old_id, session_ids)
        self.assertEqual(payload["session_window_seconds"], 86400)

    def test_get_session_runtime_log_returns_summary_and_optional_entries(self) -> None:
        append_history(
            self.runtime_root,
            username="root",
            session_id=self.session_id,
            entry={
                "direction": "event",
                "ts": "2026-05-20T13:36:00Z",
                "event_type": "runtime.status_changed",
                "text": "Runtime executing",
                "service_id": "service-codex-verify",
                "event": {"type": "runtime.status_changed", "runtime_execution_state": "running"},
            },
            limit=20,
        )

        Handler = self._make_handler(lambda **kwargs: ("", ""))
        handler = object.__new__(Handler)
        handler._require_user = lambda query=None: {"username": "root", "session_id": self.session_id}
        responses: list[tuple[int, dict]] = []
        handler._json = lambda status, payload: responses.append((status, payload))

        handler._do_GET_session_runtime_log("/session/runtime-log", {"entries": ["0"]})

        self.assertEqual(responses[-1][0], 200)
        self.assertEqual(responses[-1][1]["entries"], [])
        self.assertEqual(
            responses[-1][1]["summary"],
            {
                "entry_count": 1,
                "first_ts": "2026-05-20T13:36:00Z",
                "last_ts": "2026-05-20T13:36:00Z",
                "service_ids": ["service-codex-verify"],
                "event_types": ["runtime.status_changed"],
            },
        )

        handler._do_GET_session_runtime_log("/session/runtime-log", {"limit": ["5"]})

        self.assertEqual(responses[-1][0], 200)
        self.assertEqual(len(responses[-1][1]["entries"]), 1)
        self.assertEqual(responses[-1][1]["entries"][0]["event_type"], "runtime.status_changed")
        self.assertEqual(responses[-1][1]["entries"][0]["entry"]["text"], "Runtime executing")

    def test_get_session_runtime_log_filters_by_recent_window(self) -> None:
        append_history(
            self.runtime_root,
            username="root",
            session_id=self.session_id,
            entry={
                "direction": "event",
                "ts": "2026-05-01T00:00:00Z",
                "event_type": "runtime.status_changed",
                "text": "Old runtime event",
                "service_id": "service-codex-old",
                "event": {"type": "runtime.status_changed"},
            },
            limit=20,
        )
        append_history(
            self.runtime_root,
            username="root",
            session_id=self.session_id,
            entry={
                "direction": "event",
                "ts": "2099-05-20T13:36:00Z",
                "event_type": "runtime.status_changed",
                "text": "Recent runtime event",
                "service_id": "service-codex-new",
                "event": {"type": "runtime.status_changed"},
            },
            limit=20,
        )

        Handler = self._make_handler(lambda **kwargs: ("", ""))
        handler = object.__new__(Handler)
        handler._require_user = lambda query=None: {"username": "root", "session_id": self.session_id}
        responses: list[tuple[int, dict]] = []
        handler._json = lambda status, payload: responses.append((status, payload))

        handler._do_GET_session_runtime_log("/session/runtime-log", {"session_window_seconds": ["86400"]})

        self.assertEqual(responses[-1][0], 200)
        self.assertEqual(responses[-1][1]["summary"]["entry_count"], 1)
        self.assertEqual(responses[-1][1]["entries"][0]["entry"]["text"], "Recent runtime event")

    def test_get_messages_filters_by_recent_window(self) -> None:
        append_history(
            self.runtime_root,
            username="root",
            session_id=self.session_id,
            entry={"direction": "out", "ts": "2026-05-01T00:00:00Z", "text": "old user prompt"},
            limit=20,
        )
        append_history(
            self.runtime_root,
            username="root",
            session_id=self.session_id,
            entry={"direction": "out", "ts": "2099-05-20T13:36:00Z", "text": "recent user prompt"},
            limit=20,
        )

        Handler = self._make_handler(lambda **kwargs: ("", ""))
        handler = object.__new__(Handler)
        handler._require_user = lambda query=None: {"username": "root", "session_id": self.session_id}
        responses: list[tuple[int, dict]] = []
        handler._json = lambda status, payload: responses.append((status, payload))

        handler._do_GET_messages("/messages", {"session_window_seconds": ["86400"]})

        self.assertEqual(responses[-1][0], 200)
        self.assertEqual(
            [entry["text"] for entry in responses[-1][1]["messages"]],
            ["recent user prompt"],
        )

    def test_get_overview_cache_key_includes_session_window_seconds(self) -> None:
        old = create_conversation_session(self.runtime_root, username="root", label="Old Session")
        old_id = str(old["session_id"])
        old_record = get_session_settings(self.runtime_root, username="root", session_id=old_id)
        assert old_record is not None
        old_record["created_at"] = "2026-01-01T00:00:00Z"
        old_record["updated_at"] = "2026-01-01T00:00:00Z"
        write_json_file(
            session_metadata_path(self.runtime_root, username="root", session_id=old_id),
            old_record,
        )

        Handler = self._make_handler(lambda **kwargs: ("", ""))
        handler = object.__new__(Handler)
        handler._require_user = lambda query=None: {
            "username": "root",
            "viewer_username": "root",
            "session_id": self.session_id,
        }
        responses: list[tuple[int, dict]] = []
        handler._json = lambda status, payload: responses.append((status, payload))

        handler._do_GET_overview("/overview", {"session_window_seconds": ["86400"]})
        recent_ids = {entry["session_id"] for entry in responses[-1][1]["session_summaries"]}
        self.assertNotIn(old_id, recent_ids)

        handler._do_GET_overview("/overview", {"session_window_seconds": ["0"]})
        all_ids = {entry["session_id"] for entry in responses[-1][1]["session_summaries"]}
        self.assertIn(old_id, all_ids)
