from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from runtime.agent_service import (
    maybe_dispatch_panic_recovery_parent_resume,
)
from runtime.panic_recovery import ensure_panic_recovery_session
from runtime.persistent_state import (
    create_conversation_session,
    create_child_conversation_session,
    get_session_settings,
    list_session_children,
    list_session_parents,
    load_pending_inputs,
    read_jsonl,
)
from runtime.service_control import build_prompt, parse_service_response


TEST_USERNAME = "test-user"


class ServiceControlParserTests(unittest.TestCase):
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


class PanicRecoveryDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.runtime_root = Path(self.tempdir.name)
        self.username = TEST_USERNAME
        self.manifest = {"node_id": "node-aize"}

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_parent_resume_dispatch_adds_restart_input_and_dispatch_message(self) -> None:
        source_session = create_conversation_session(
            self.runtime_root,
            username=self.username,
            label="Source Session",
        )
        source_session_id = str(source_session["session_id"])

        recovery_session = create_child_conversation_session(
            self.runtime_root,
            username=self.username,
            parent_session_id=source_session_id,
            label="Recovery Session",
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
