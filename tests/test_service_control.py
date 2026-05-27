from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys
import json

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from runtime.agent_service import (
    _canonical_spawn_handoff_parent_session_id,
    _completed_recovery_audit_if_parent_resumed,
    _finalize_superseded_panic_recovery_siblings,
    _handoff_spawn_request_to_child_session,
    _interactive_recent_context,
    _is_usage_limit_error_text,
    _route_spawn_request_to_communication_child_session,
    _retry_after_seconds_from_error_text,
    _service_can_spawn_children,
    _worker_session_skills_block,
    maybe_dispatch_panic_recovery_parent_resume,
)
from runtime.panic_recovery import ensure_panic_recovery_session
from runtime.persistent_state_pkg import (
    append_history as append_user_history,
    create_conversation_session,
    create_child_conversation_session,
    get_session_settings,
    update_session_skills,
    update_session_goal_flags,
    list_session_children,
    list_session_parents,
    load_pending_inputs,
    read_jsonl,
)
from runtime.service_control import (
    build_interactive_prompt,
    build_prompt,
    extract_agent_message_visible_text,
    parse_service_response,
    parse_service_response_with_fallback,
)


TEST_USERNAME = "test-user"


class ServiceControlParserTests(unittest.TestCase):
    def test_service_can_spawn_children_accepts_auth_or_owner_capability(self) -> None:
        self.assertTrue(
            _service_can_spawn_children(
                self_service={"owner_capabilities": []},
                auth_context={"capabilities": ["spawn_service"]},
            )
        )
        self.assertTrue(
            _service_can_spawn_children(
                self_service={"owner_capabilities": ["spawn_service"]},
                auth_context={"capabilities": []},
            )
        )
        self.assertFalse(
            _service_can_spawn_children(
                self_service={"owner_capabilities": []},
                auth_context={"capabilities": []},
            )
        )

    def test_build_interactive_prompt_includes_recent_session_context(self) -> None:
        prompt = build_interactive_prompt(
            text="状況を教えて",
            username=TEST_USERNAME,
            session_id="session-1",
            recent_context=[
                {
                    "role": "GoalManager(service-codex-001)",
                    "ts": "2026-05-04T01:00:00Z",
                    "text": "GoalManager marked routing incomplete.",
                },
                {
                    "role": "Agent(service-codex-002)",
                    "ts": "2026-05-04T01:01:00Z",
                    "text": "Worker found the latest diff in http_handler.py.",
                },
            ],
        )

        self.assertIn("<aize_recent_session_context>", prompt)
        self.assertIn("GoalManager(service-codex-001)", prompt)
        self.assertIn("Worker found the latest diff", prompt)
        self.assertIn("Think normally before answering", prompt)
        self.assertIn("do not inspect files, run shell commands, browse, or use tools", prompt)
        self.assertIn("WorkerAgent is already running in parallel", prompt)
        self.assertIn("Do not frame your reply as a future proposal", prompt)

    def test_usage_limit_helpers_treat_capacity_as_transient(self) -> None:
        error_text = "Selected model is at capacity. Please try a different model."
        self.assertTrue(_is_usage_limit_error_text(error_text))
        self.assertEqual(_retry_after_seconds_from_error_text(error_text), 15 * 60)

    def test_interactive_recent_context_keeps_worker_and_goal_manager_results(self) -> None:
        context = _interactive_recent_context(
            [
                {"direction": "event", "event_type": "agent.turn_started", "text": "Agent started"},
                {
                    "direction": "agent",
                    "event_type": "service.goal_audit_completed",
                    "service_id": "service-codex-001",
                    "text": "Goal still needs routing verification.",
                    "ts": "2026-05-04T01:00:00Z",
                },
                {
                    "direction": "in",
                    "service_id": "service-codex-002",
                    "text": "Worker verified that the target session was not updated.",
                    "ts": "2026-05-04T01:01:00Z",
                },
            ]
        )

        self.assertEqual(len(context), 2)
        self.assertEqual(context[0]["role"], "GoalManager(service-codex-001)")
        self.assertEqual(context[1]["role"], "Agent(service-codex-002)")
        self.assertIn("target session", context[1]["text"])

    def test_parse_service_response_rejects_missing_comma_with_json_decode_shape(self) -> None:
        malformed = '{"assistant_text":"ok" "spawn_requests":[]}'
        with self.assertRaisesRegex(
            RuntimeError,
            r"invalid JSON output for service_control_v1: Expecting ',' delimiter",
        ) as context:
            parse_service_response(malformed, "service_control_v1")
        self.assertIn("Expecting ',' delimiter", str(context.exception))

    def test_parse_service_response_parses_embedded_candidate(self) -> None:
        wrapped = 'noise before {"assistant_text":"ok", "spawn_requests":[]} noise after'
        text, spawn_requests = parse_service_response(wrapped, "service_control_v1")
        self.assertEqual(text, "ok")
        self.assertEqual(spawn_requests, [])

    def test_parse_service_response_with_fallback_keeps_plain_text(self) -> None:
        text, spawn_requests, error = parse_service_response_with_fallback(
            "plain text progress update",
            "service_control_v1",
        )
        self.assertEqual(text, "plain text progress update")
        self.assertEqual(spawn_requests, [])
        self.assertIn("invalid JSON output for service_control_v1", str(error))

    def test_parse_service_response_with_fallback_extracts_assistant_text_shape(self) -> None:
        text, spawn_requests, error = parse_service_response_with_fallback(
            '{"AssistantText":"本文だけ表示する","spawn_requests":[]}',
            "service_control_v1",
        )
        self.assertEqual(text, "本文だけ表示する")
        self.assertEqual(spawn_requests, [])
        self.assertIsNotNone(error)

    def test_extract_agent_message_visible_text_strips_control_json(self) -> None:
        self.assertEqual(
            extract_agent_message_visible_text('{"assistant_text":"本文","spawn_requests":[]}'),
            "本文",
        )
        self.assertEqual(
            extract_agent_message_visible_text('{"AssistantText":"本文","spawn_requests":[]}'),
            "本文",
        )

    def test_build_prompt_spells_out_spawn_request_shape(self) -> None:
        prompt = build_prompt(
            {
                "persona": "Test persona",
                "max_turns": 100,
                "response_schema_id": "service_control_v1",
            },
            {"display_name": "HttpBridge"},
            "<aize_input_batch />",
            6,
        )
        self.assertIn('"service": {...}', prompt)
        self.assertIn('"allowed_peers": [...]', prompt)
        self.assertIn('"initial_prompt": "..."', prompt)
        self.assertIn('"service_type"', prompt)

    def test_build_prompt_treats_negative_max_turns_as_unlimited(self) -> None:
        prompt = build_prompt(
            {
                "persona": "Test persona",
                "max_turns": -1,
                "response_schema_id": None,
            },
            {"display_name": "HttpBridge"},
            "hello",
            6,
        )
        self.assertIn("There is no max-turn limit for this service.", prompt)
        self.assertNotIn("reply number 6 of -1", prompt)

    def test_worker_session_skills_block_includes_manifest_and_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            session = create_conversation_session(runtime_root, username=TEST_USERNAME, label="Dev")
            session_id = str(session["session_id"])
            update_session_skills(
                runtime_root,
                username=TEST_USERNAME,
                session_id=session_id,
                session_skills=[
                    {
                        "skill_id": "dev-routing",
                        "title": "Dev Routing",
                        "when_to_use": "Use this session for implementation work delegated out of routing.",
                        "usage": "Implement the change here and do not bounce it back to the router.",
                        "prompt": "Route implementation work here.",
                        "files": [
                            {
                                "path": "README.md",
                                "content": "Use this session for implementation work.",
                            }
                        ],
                    }
                ],
            )

            block = _worker_session_skills_block(
                runtime_root,
                username=TEST_USERNAME,
                session_id=session_id,
            )

        self.assertIn("<aize_session_skills>", block)
        self.assertIn("Route implementation work here.", block)
        self.assertIn("Use this session for implementation work delegated out of routing.", block)
        self.assertIn("Implement the change here and do not bounce it back to the router.", block)
        self.assertIn('path="README.md"', block)
        self.assertIn("Use this session for implementation work.", block)

    def test_handoff_spawn_request_to_child_session_materializes_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            session = create_conversation_session(
                runtime_root,
                username=TEST_USERNAME,
                label="Parent",
            )
            session_id = str(session["session_id"])
            dispatched: list[str] = []

            created = _handoff_spawn_request_to_child_session(
                runtime_root=runtime_root,
                username=TEST_USERNAME,
                session_id=session_id,
                goal_manager_service_id="service-codex-001",
                control={
                    "service": {
                        "service_id": "service-codex-helper-001",
                        "kind": "codex",
                        "display_name": "Focused Helper",
                    },
                    "initial_prompt": "Investigate the routing failure and report back.",
                },
                dispatch_child_session=lambda child_session_id: (
                    dispatched.append(child_session_id) or "service-codex-001"
                ),
            )

            self.assertEqual(len(created), 1)
            self.assertEqual(created[0]["label"], "Focused Helper")
            self.assertEqual(created[0]["dispatch_service_id"], "service-codex-001")
            self.assertEqual(len(dispatched), 1)
            child_session = get_session_settings(
                runtime_root,
                username=TEST_USERNAME,
                session_id=created[0]["session_id"],
            )
            assert child_session is not None
            self.assertEqual(child_session.get("parent_session_id"), session_id)
            self.assertEqual(child_session.get("goal_text"), "Investigate the routing failure and report back.")

    def test_handoff_spawn_request_uses_canonical_development_parent_for_communication_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            route_skill = {
                "skill_id": "canonical-development-routing",
                "routing_mode": "create_child_session",
                "canonical_session_key": "aize.development",
                "route_parent_scope": "root_session",
                "target_template_id": "aize-development.bug-hunting",
                "target_label": "AIze Development",
            }
            entrance = create_conversation_session(
                runtime_root,
                username=TEST_USERNAME,
                label="Entrance",
                session_ui_mode="communication",
                communication_agent_enabled=True,
                session_skills=[route_skill],
            )
            canonical_parent = create_conversation_session(
                runtime_root,
                username=TEST_USERNAME,
                label="AIze Development",
                session_skills=[
                    {
                        "skill_id": "aize-development-session",
                        "canonical_session_key": "aize.development",
                    }
                ],
            )
            update_session_goal_flags(
                runtime_root,
                username=TEST_USERNAME,
                session_id=str(canonical_parent["session_id"]),
                goal_active=True,
                goal_completed=False,
                goal_progress_state="in_progress",
            )
            stored_parent = get_session_settings(
                runtime_root,
                username=TEST_USERNAME,
                session_id=str(canonical_parent["session_id"]),
            )
            assert stored_parent is not None
            stored_parent["parent_session_id"] = "default"
            stored_parent["launcher_template_id"] = "aize-development.bug-hunting"
            stored_parent["launcher_unit_id"] = "aize-development.bug-hunting"
            canonical_parent_path = next(
                runtime_root.glob(f"**/{canonical_parent['session_id']}/session.json")
            )
            canonical_parent_path.write_text(json.dumps(stored_parent, indent=2) + "\n", encoding="utf-8")

            created = _handoff_spawn_request_to_child_session(
                runtime_root=runtime_root,
                username=TEST_USERNAME,
                session_id=str(entrance["session_id"]),
                goal_manager_service_id="service-codex-001",
                control={
                    "service": {
                        "service_id": "service-codex-helper-001",
                        "kind": "codex",
                        "display_name": "Focused Helper",
                    },
                    "initial_prompt": "Fix the delegated routing path.",
                },
            )

            self.assertEqual(len(created), 1)
            child_session = get_session_settings(
                runtime_root,
                username=TEST_USERNAME,
                session_id=created[0]["session_id"],
            )
            assert child_session is not None
            self.assertEqual(child_session.get("parent_session_id"), canonical_parent["session_id"])

    def test_communication_spawn_handoff_uses_canonical_development_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            entrance = create_conversation_session(
                runtime_root,
                username=TEST_USERNAME,
                label="Entrance",
                session_ui_mode="communication",
                communication_agent_enabled=True,
                session_skills=[
                    {
                        "skill_id": "canonical-development-routing",
                        "routing_mode": "create_child_session",
                        "canonical_session_key": "aize.development",
                        "target_label": "AIze Development",
                    }
                ],
            )
            canonical_parent = create_conversation_session(
                runtime_root,
                username=TEST_USERNAME,
                label="AIze Development",
                session_group="root",
                session_skills=[
                    {
                        "skill_id": "aize-development-session",
                        "canonical_session_key": "aize.development",
                    }
                ],
            )
            compatibility_parent = create_conversation_session(
                runtime_root,
                username=TEST_USERNAME,
                label="AIze Development",
                session_group="user",
                session_skills=[
                    {
                        "skill_id": "legacy-development-session",
                        "canonical_session_key": "aize.development",
                    }
                ],
            )

            update_session_goal_flags(
                runtime_root,
                username=TEST_USERNAME,
                session_id=str(canonical_parent["session_id"]),
                goal_active=True,
                goal_completed=False,
                goal_progress_state="in_progress",
            )
            update_session_goal_flags(
                runtime_root,
                username=TEST_USERNAME,
                session_id=str(compatibility_parent["session_id"]),
                goal_active=True,
                goal_completed=False,
                goal_progress_state="in_progress",
            )

            resolved = _canonical_spawn_handoff_parent_session_id(
                runtime_root=runtime_root,
                username=TEST_USERNAME,
                session_id=str(entrance["session_id"]),
            )

            self.assertEqual(resolved, canonical_parent["session_id"])

    def test_route_spawn_request_to_communication_child_session_creates_canonical_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            entrance = create_conversation_session(
                runtime_root,
                username=TEST_USERNAME,
                label="Entrance",
                session_ui_mode="communication",
                communication_agent_enabled=True,
                session_skills=[
                    {
                        "skill_id": "canonical-development-routing",
                        "routing_mode": "create_child_session",
                        "route_when_unhandled": False,
                        "canonical_session_key": "aize.development",
                        "route_parent_scope": "root_session",
                        "target_template_id": "aize-development.bug-hunting",
                        "target_label": "AIze Development",
                        "target_child_label": "AIze Development Task",
                        "target_goal_text": "Implement the requested changes here.",
                        "preferred_provider": "codex",
                        "selected_agents": ["codex_pool"],
                        "spawn_session_skills": [
                            {
                                "skill_id": "aize-development-session",
                                "canonical_session_key": "aize.development",
                            }
                        ],
                    }
                ],
            )
            dispatched: list[str] = []
            created = _route_spawn_request_to_communication_child_session(
                runtime_root=runtime_root,
                username=TEST_USERNAME,
                session_id=str(entrance["session_id"]),
                goal_manager_service_id="service-codex-001",
                control={
                    "service": {
                        "service_id": "service-codex-helper-001",
                        "kind": "codex",
                        "display_name": "Focused Helper",
                    },
                    "initial_prompt": "Fix the delegated routing path.",
                },
                dispatch_child_session=lambda child_session_id: (
                    dispatched.append(child_session_id) or "service-codex-001"
                ),
            )

            self.assertEqual(len(created), 1)
            self.assertEqual(len(dispatched), 1)
            child_session = get_session_settings(
                runtime_root,
                username=TEST_USERNAME,
                session_id=created[0]["session_id"],
            )
            assert child_session is not None
            self.assertEqual(child_session.get("goal_text"), "Fix the delegated routing path.")
            parent_session = get_session_settings(
                runtime_root,
                username=TEST_USERNAME,
                session_id=str(child_session.get("parent_session_id") or ""),
            )
            assert parent_session is not None
            self.assertEqual(parent_session.get("label"), "AIze Development")
            self.assertEqual(parent_session.get("parent_session_id"), "default")


class PanicRecoveryReturnPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.runtime_root = Path(self.tempdir.name)
        self.username = TEST_USERNAME

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_ensure_panic_recovery_session_creates_parent_child_links(self) -> None:
        source_session = create_conversation_session(self.runtime_root, username=self.username, label="Source")
        source_session_id = str(source_session["session_id"])

        recovery = ensure_panic_recovery_session(
            self.runtime_root,
            username=self.username,
            source_session_id=source_session_id,
            source_label="Source",
            panic_service_id="service-codex-001",
            event={
                "type": "service.post_message_out_failed",
                "error": "RuntimeError(\"invalid JSON output for service_control_v1\")",
                "reply_index": 1,
                "provider": "codex",
            },
            preferred_provider="codex",
        )
        self.assertIsInstance(recovery, dict)
        recovery_session_id = str(recovery["session_id"])

        recovery_session = get_session_settings(
            self.runtime_root,
            username=self.username,
            session_id=recovery_session_id,
        )
        self.assertIsNotNone(recovery_session)
        assert recovery_session is not None
        self.assertEqual(
            str(recovery_session.get("parent_session_id") or ""),
            source_session_id,
            "recovery session must preserve source session parent linkage",
        )
        self.assertEqual(
            list_session_parents(
                self.runtime_root,
                username=self.username,
                session_id=recovery_session_id,
            ),
            [source_session_id],
            "recovery session DAG parent index should include source session",
        )
        self.assertEqual(
            list_session_children(
                self.runtime_root,
                username=self.username,
                session_id=source_session_id,
            ),
            [recovery_session_id],
            "source session DAG child index should include recovery session",
        )

    def test_ensure_panic_recovery_session_reuses_active_recovery_for_changed_transport_signature(self) -> None:
        source_session = create_conversation_session(self.runtime_root, username=self.username, label="Source")
        source_session_id = str(source_session["session_id"])

        first = ensure_panic_recovery_session(
            self.runtime_root,
            username=self.username,
            source_session_id=source_session_id,
            source_label="Source",
            panic_service_id="service-codex-001",
            event={
                "type": "service.worker_failed",
                "error": "RuntimeError('stream disconnected before completion: failed to lookup address information')",
                "reply_index": 1,
            },
            preferred_provider="codex",
        )
        second = ensure_panic_recovery_session(
            self.runtime_root,
            username=self.username,
            source_session_id=source_session_id,
            source_label="Source",
            panic_service_id="service-codex-001",
            event={
                "type": "service.worker_failed",
                "error": "RuntimeError('stream disconnected before completion: error sending request for url')",
                "reply_index": 2,
            },
            preferred_provider="codex",
        )

        self.assertIsInstance(first, dict)
        self.assertIsInstance(second, dict)
        self.assertEqual(first["session_id"], second["session_id"])
        self.assertEqual(
            list_session_children(self.runtime_root, username=self.username, session_id=source_session_id),
            [first["session_id"]],
        )

    def test_ensure_panic_recovery_session_creates_new_after_prior_recovery_completed(self) -> None:
        source_session = create_conversation_session(self.runtime_root, username=self.username, label="Source")
        source_session_id = str(source_session["session_id"])
        first = ensure_panic_recovery_session(
            self.runtime_root,
            username=self.username,
            source_session_id=source_session_id,
            source_label="Source",
            panic_service_id="service-codex-001",
            event={"type": "service.worker_failed", "error": "first"},
            preferred_provider="codex",
        )
        assert first is not None
        update_session_goal_flags(
            self.runtime_root,
            username=self.username,
            session_id=str(first["session_id"]),
            goal_completed=True,
            goal_progress_state="complete",
        )

        second = ensure_panic_recovery_session(
            self.runtime_root,
            username=self.username,
            source_session_id=source_session_id,
            source_label="Source",
            panic_service_id="service-codex-001",
            event={"type": "service.worker_failed", "error": "second"},
            preferred_provider="codex",
        )

        assert second is not None
        self.assertNotEqual(first["session_id"], second["session_id"])
        self.assertEqual(
            list_session_children(self.runtime_root, username=self.username, session_id=source_session_id),
            [first["session_id"], second["session_id"]],
        )

    def test_transport_like_codex_panic_selects_non_codex_recovery_provider(self) -> None:
        source_session = create_conversation_session(self.runtime_root, username=self.username, label="Source")
        source_session_id = str(source_session["session_id"])

        recovery = ensure_panic_recovery_session(
            self.runtime_root,
            username=self.username,
            source_session_id=source_session_id,
            source_label="Source",
            panic_service_id="service-codex-001",
            event={
                "type": "service.worker_failed",
                "error": "worker quit with fatal: Transport channel closed; error sending request for url",
            },
            preferred_provider="codex",
        )

        assert recovery is not None
        self.assertEqual(recovery.get("preferred_provider"), "claude")

    def test_completed_recovery_finalizes_stale_active_siblings_for_same_panic(self) -> None:
        source_session = create_conversation_session(self.runtime_root, username=self.username, label="Source")
        source_session_id = str(source_session["session_id"])
        first = ensure_panic_recovery_session(
            self.runtime_root,
            username=self.username,
            source_session_id=source_session_id,
            source_label="Source",
            panic_service_id="service-codex-001",
            event={"type": "service.worker_failed", "error": "first"},
            preferred_provider="codex",
        )
        assert first is not None
        update_session_goal_flags(
            self.runtime_root,
            username=self.username,
            session_id=str(first["session_id"]),
            goal_completed=True,
            goal_progress_state="complete",
        )
        second = ensure_panic_recovery_session(
            self.runtime_root,
            username=self.username,
            source_session_id=source_session_id,
            source_label="Source",
            panic_service_id="service-codex-001",
            event={"type": "service.worker_failed", "error": "second"},
            preferred_provider="codex",
        )
        assert second is not None
        update_session_goal_flags(
            self.runtime_root,
            username=self.username,
            session_id=str(first["session_id"]),
            goal_active=True,
            goal_completed=False,
            goal_progress_state="in_progress",
        )

        finalized = _finalize_superseded_panic_recovery_siblings(
            runtime_root=self.runtime_root,
            username=self.username,
            completed_recovery_session_id=str(second["session_id"]),
            completed_recovery_session_settings=second,
            completion_service_id="service-codex-002",
        )

        self.assertEqual(finalized, [first["session_id"]])
        stale = get_session_settings(self.runtime_root, username=self.username, session_id=str(first["session_id"]))
        assert stale is not None
        self.assertFalse(stale["goal_active"])
        self.assertTrue(stale["goal_completed"])
        self.assertEqual(stale["goal_progress_state"], "complete")


class PanicRecoveryDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.runtime_root = Path(self.tempdir.name)
        self.username = TEST_USERNAME
        self.manifest = {"node_id": "node-aize"}

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_completed_recovery_audit_requires_parent_resume(self) -> None:
        source_session = create_conversation_session(
            self.runtime_root,
            username=self.username,
            label="Source Session",
        )
        source_session_id = str(source_session["session_id"])
        recovery = ensure_panic_recovery_session(
            self.runtime_root,
            username=self.username,
            source_session_id=source_session_id,
            source_label="Source Session",
            panic_service_id="service-codex-001",
            event={"type": "service.worker_failed", "error": "boom"},
            preferred_provider="codex",
        )
        assert recovery is not None
        recovery_session_id = str(recovery["session_id"])

        audit = _completed_recovery_audit_if_parent_resumed(
            runtime_root=self.runtime_root,
            username=self.username,
            session_id=recovery_session_id,
            recovery_session_settings=recovery,
            goal_id=str(recovery.get("goal_id") or ""),
            goal_text=str(recovery.get("goal_text") or ""),
        )

        self.assertIsNone(audit)

    def test_completed_recovery_audit_detects_parent_resume(self) -> None:
        source_session = create_conversation_session(
            self.runtime_root,
            username=self.username,
            label="Source Session",
        )
        source_session_id = str(source_session["session_id"])
        recovery = ensure_panic_recovery_session(
            self.runtime_root,
            username=self.username,
            source_session_id=source_session_id,
            source_label="Source Session",
            panic_service_id="service-codex-001",
            event={"type": "service.worker_failed", "error": "boom"},
            preferred_provider="codex",
        )
        assert recovery is not None
        recovery_session_id = str(recovery["session_id"])

        append_user_history(
            self.runtime_root,
            username=self.username,
            session_id=source_session_id,
            entry={
                "direction": "in",
                "ts": "9999-12-31T23:59:59Z",
                "from": "service-codex-001",
                "session_id": source_session_id,
                "text": "Parent resumed after recovery.",
            },
            limit=50,
        )

        audit = _completed_recovery_audit_if_parent_resumed(
            runtime_root=self.runtime_root,
            username=self.username,
            session_id=recovery_session_id,
            recovery_session_settings=recovery,
            goal_id=str(recovery.get("goal_id") or ""),
            goal_text=str(recovery.get("goal_text") or ""),
        )

        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["progress_state"], "complete")
        self.assertTrue(audit["goal_satisfied"])
        self.assertIn("parent session already resumed", audit["summary"])

    def test_parent_resume_dispatch_adds_restart_input_and_dispatch_message(self) -> None:
        source_session = create_conversation_session(
            self.runtime_root,
            username=self.username,
            label="Source Session",
        )
        source_session_id = str(source_session["session_id"])

        recovery_session = ensure_panic_recovery_session(
            self.runtime_root,
            username=self.username,
            source_session_id=source_session_id,
            source_label="Source Session",
            panic_service_id="service-codex-001",
            event={"type": "service.worker_failed", "error": "boom"},
            preferred_provider="codex",
        )
        self.assertIsNotNone(recovery_session)
        recovery_session = recovery_session or {}
        recovery_session_id = str(recovery_session.get("session_id"))
        self.assertTrue(recovery_session_id)

        batch_xml = (
            '<aize_input_batch><inputs>'
            '<input index="1" kind="panic_recovery">'
            "Parent-resume handshake"
            "</input></inputs></aize_input_batch>"
        )
        sent_messages: list[dict] = []

        def send_tx(message: dict) -> None:
            sent_messages.append(message)

        log_path = self.runtime_root / "agent.log"
        session_settings = get_session_settings(
            self.runtime_root,
            username=self.username,
            session_id=recovery_session_id,
        ) or {}
        maybe_dispatch_panic_recovery_parent_resume(
            incoming_text=batch_xml,
            runtime_root=self.runtime_root,
            manifest=self.manifest,
            service_id="service-codex-001",
            process_id="proc-test",
            log_path=log_path,
            send_tx=send_tx,
            scope_username=self.username,
            scope_session_id=recovery_session_id,
            session_settings=session_settings,
        )

        parent_pending = load_pending_inputs(
            self.runtime_root,
            username=self.username,
            session_id=source_session_id,
        )
        self.assertTrue(parent_pending, "parent should receive restart_resume pending input")
        restart_items = [item for item in parent_pending if item.get("kind") == "restart_resume"]
        self.assertEqual(len(restart_items), 1)
        restart_text = str(restart_items[0].get("text") or "")
        self.assertIn(recovery_session_id, restart_text)
        self.assertIn("<aize_panic_recovery_parent_resume>", restart_text)

        self.assertEqual(len(sent_messages), 1)
        self.assertEqual(sent_messages[0].get("payload", {}).get("reason"), "panic_recovery_parent_resume")

        log_entries = list(read_jsonl(log_path))
        self.assertTrue(any(entry.get("type") == "service.panic_recovery_parent_resume_dispatched" for entry in log_entries))

    def test_parent_resume_dispatch_does_not_require_bootstrap_input_after_recovery_progress(self) -> None:
        source_session = create_conversation_session(
            self.runtime_root,
            username=self.username,
            label="Source Session",
        )
        source_session_id = str(source_session["session_id"])

        recovery_session = ensure_panic_recovery_session(
            self.runtime_root,
            username=self.username,
            source_session_id=source_session_id,
            source_label="Source Session",
            panic_service_id="service-codex-001",
            event={"type": "service.worker_failed", "error": "boom"},
            preferred_provider="codex",
        )
        self.assertIsNotNone(recovery_session)
        recovery_session = recovery_session or {}
        recovery_session_id = str(recovery_session.get("session_id"))
        self.assertTrue(recovery_session_id)

        sent_messages: list[dict] = []

        def send_tx(message: dict) -> None:
            sent_messages.append(message)

        log_path = self.runtime_root / "agent.log"
        session_settings = get_session_settings(
            self.runtime_root,
            username=self.username,
            session_id=recovery_session_id,
        ) or {}
        maybe_dispatch_panic_recovery_parent_resume(
            incoming_text="<aize_input_batch><inputs></inputs></aize_input_batch>",
            runtime_root=self.runtime_root,
            manifest=self.manifest,
            service_id="service-codex-001",
            process_id="proc-test",
            log_path=log_path,
            send_tx=send_tx,
            scope_username=self.username,
            scope_session_id=recovery_session_id,
            session_settings=session_settings,
        )

        parent_pending = load_pending_inputs(
            self.runtime_root,
            username=self.username,
            session_id=source_session_id,
        )
        restart_items = [item for item in parent_pending if item.get("kind") == "restart_resume"]
        self.assertEqual(len(restart_items), 1)
        self.assertEqual(len(sent_messages), 1)
