from __future__ import annotations

import json
import ast
import sys
import tempfile
import unittest
from pathlib import Path
from io import StringIO
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from runtime.cli_service_adapter import (  # noqa: E402
    active_agent_turn_state,
    build_session_runtime_summary,
    collect_and_verify_turn_completed_artifacts,
    build_goal_audit_prompt,
    build_worker_count_summary,
    build_progress_inquiry_xml,
    build_goal_audit_log_bundle,
    dispatch_pending_opens_visible_turn,
    goal_followup_dispatch_targets,
    goal_audit_should_enqueue_agent_followup,
    goal_audit_history_text,
    goal_state_response_payload,
    handle_goal_manager_compact_request,
    latest_goal_manager_runtime_state,
    manual_compact_clears_audit_state,
    maybe_enqueue_mid_turn_progress_inquiry,
    maybe_release_session_provider,
    pending_turn_completed_events_since_last_review,
    persist_goal_audit_completion,
    pending_progress_inquiry_exists,
    run_goal_audit,
    summarize_provider_event,
)
from kernel.lifecycle import init_lifecycle_state, register_process  # noqa: E402
from kernel.auth import bootstrap_root_user, has_users as auth_has_users, resolve_user_record, verify_user_password  # noqa: E402
from kernel.registry import init_registry, update_service_process  # noqa: E402
from runtime.providers.claude import normalize_claude_stream_event, run_claude  # noqa: E402
from runtime.panic_recovery import ensure_panic_recovery_session  # noqa: E402
from runtime.compaction import maybe_resume_after_restart  # noqa: E402
from runtime.agent_service import (  # noqa: E402
    _materialize_goal_child_sessions,
    _extract_user_response_wait_control,
    _should_defer_dispatch_for_completed_goal,
)
from runtime.ws_peer_client import _remote_session_entry_to_dispatch  # noqa: E402
from runtime.persistent_state import (  # noqa: E402
    add_session_child,
    append_history,
    append_goal_manager_pending_input,
    consume_session_due_user_response_wait,
    complete_session_child,
    create_conversation_session,
    create_session,
    create_child_conversation_session,
    ensure_state,
    get_history,
    list_session_agent_contacts,
    list_active_in_progress_child_sessions,
    list_session_children,
    list_session_parents,
    get_session_settings,
    load_goal_manager_pending_inputs,
    lease_session_service,
    list_codex_sessions,
    load_pending_inputs,
    record_session_agent_contact,
    release_nonrunnable_session_services,
    save_codex_session,
    session_ui_mode,
    session_operation_allowed,
    session_goal_context,
    session_dag_children_path,
    session_dag_parents_path,
    session_goal_manager_pending_path,
    session_goal_manager_reviews_path,
    session_goal_manager_state_path,
    session_metadata_path,
    session_service_state_path,
    session_timeline_path,
    state_path,
    update_session_goal,
    update_session_goal_flags,
    update_session_user_response_wait,
    write_json_file,
)
from runtime.service_control import build_prompt  # noqa: E402
from runtime.message_builder import build_aize_input_batch_xml, make_aize_pending_input  # noqa: E402


TEST_USERNAME = "test-user"


class GoalManagerCompactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.runtime_root = Path(self.tempdir.name) / "runtime"
        ensure_state(self.runtime_root)
        (self.runtime_root / "logs").mkdir(parents=True, exist_ok=True)
        talk = create_conversation_session(self.runtime_root, username=TEST_USERNAME, label="Goal Talk")
        self.session_id = str(talk["session_id"])

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_goal_audit_schema_requires_all_agent_directive_properties(self) -> None:
        schema = json.loads((ROOT / "src" / "runtime" / "schemas" / "goal_audit_v1.json").read_text())
        self.assertEqual(
            sorted(schema["required"]),
            sorted(schema["properties"].keys()),
        )
        directive_items = schema["properties"]["agent_directives"]["items"]
        self.assertEqual(
            sorted(directive_items["required"]),
            sorted(directive_items["properties"].keys()),
        )

    def test_build_goal_audit_log_bundle_includes_scoped_and_same_run_records(self) -> None:
        records = [
            {
                "ts": "2026-03-19T09:00:00Z",
                "type": "service.goal_audit_started",
                "scope": {"username": TEST_USERNAME, "session_id": self.session_id},
                "run_id": "run-1",
            },
            {
                "ts": "2026-03-19T09:00:01Z",
                "type": "service.worker_detail",
                "run_id": "run-1",
            },
            {
                "ts": "2026-03-19T09:00:02Z",
                "type": "service.unrelated",
                "run_id": "run-2",
            },
        ]
        log_path = self.runtime_root / "logs" / "service-codex-001.jsonl"
        with log_path.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")

        bundle_path, count = build_goal_audit_log_bundle(
            runtime_root=self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
        )

        self.assertEqual(count, 2)
        bundled = [json.loads(line) for line in bundle_path.read_text(encoding="utf-8").splitlines()]
        bundled_types = [item["record"]["type"] for item in bundled]
        self.assertEqual(bundled_types, ["service.goal_audit_started", "service.worker_detail"])

    def test_goal_audit_history_text_includes_compact_reason(self) -> None:
        text = goal_audit_history_text(
            {
                "goal_audit_session_id": "session-1",
                "progress_state": "in_progress",
                "audit_state": "needs_compact",
                "goal_satisfied": False,
                "summary": "Still incomplete",
                "request_compact": True,
                "request_compact_reason": "Repeatedly ignored guidance",
            }
        )
        self.assertIn("Still incomplete", text)
        self.assertIn("Compact requested: Repeatedly ignored guidance", text)

    def test_build_goal_audit_prompt_mentions_multi_agent_turncompleted_review(self) -> None:
        prompt = build_goal_audit_prompt(
            goal_text="Ship it",
            log_bundle_path=Path("/tmp/goal-audit.jsonl"),
            log_record_count=12,
            contacted_agents=[
                {
                    "service_id": "service-codex-001",
                    "provider": "codex",
                    "welcomed_at": "2026-03-20T12:00:00Z",
                    "last_turn_completed_at": "2026-03-20T12:05:00Z",
                }
            ],
            pending_turn_completed_events=[
                {
                    "service_id": "service-codex-001",
                    "provider": "codex",
                    "status": "success",
                    "completed_at": "2026-03-20T12:05:00Z",
                    "reply_index": "2",
                    "text": "Turn completed",
                }
            ],
            last_reviewed_turn_completed_at="2026-03-20T12:01:00Z",
            agent_welcome_enabled=True,
            verified_artifacts=[
                {
                    "service_id": "service-codex-001",
                    "reference": "file:///tmp/report.json",
                    "validation": "json_valid",
                }
            ],
            session_dir_path=Path("/tmp/session"),
            timeline_path=Path("/tmp/session/timeline.jsonl"),
            goal_context=[
                {
                    "goal_id": "goal-1",
                    "goal_text": "Root goal",
                    "goal_created_at": "2026-03-20T12:00:00Z",
                }
            ],
        )

        self.assertIn("multiple agents mixed together", prompt)
        self.assertIn("Aggregate every TurnCompleted", prompt)
        self.assertIn("Session-level additional-agent welcome signal: enabled.", prompt)
        self.assertIn('"service_id": "service-codex-001"', prompt)
        self.assertIn("Verified artifact results", prompt)
        self.assertIn("agent_directives", prompt)
        self.assertIn("at least two parallel child goals", prompt)
        self.assertIn("Session files:", prompt)
        self.assertIn("/tmp/session/timeline.jsonl", prompt)
        self.assertIn('"goal_id": "goal-1"', prompt)
        self.assertNotIn("recent history", prompt)

    def test_goal_audit_schema_rejects_single_child_goal_request(self) -> None:
        schema = json.loads((ROOT / "src" / "runtime" / "schemas" / "goal_audit_v1.json").read_text())
        child_goal_requests_schema = schema["properties"]["child_goal_requests"]
        branches = child_goal_requests_schema["anyOf"]
        self.assertEqual(len(branches), 2)
        self.assertEqual(branches[0]["maxItems"], 0)
        self.assertEqual(branches[1]["minItems"], 2)
        for branch in branches:
            self.assertEqual(branch["type"], "array")
            self.assertEqual(branch["items"], child_goal_requests_schema["items"])

    def test_run_goal_audit_parses_markdown_fenced_json(self) -> None:
        with patch(
            "runtime.cli_service_adapter.run_claude",
            return_value=(
                "```json\n"
                "{\n"
                '  "progress_state": "in_progress",\n'
                '  "audit_state": "all_clear",\n'
                '  "summary": "still working",\n'
                '  "continue_xml": "<aize_goal_feedback />",\n'
                '  "request_compact": false,\n'
                '  "request_compact_reason": "",\n'
                '  "agent_directives": []\n'
                "}\n"
                "```",
                [],
                "audit-session-1",
            ),
        ):
            audit = run_goal_audit(
                runtime_root=self.runtime_root,
                username=TEST_USERNAME,
                session_id=self.session_id,
                goal_text="Ship it",
                history_entries=[],
                provider_kind="claude",
            )

        self.assertEqual(audit["goal_audit_session_id"], "audit-session-1")
        self.assertEqual(audit["progress_state"], "in_progress")
        self.assertEqual(audit["audit_state"], "all_clear")
        self.assertEqual(audit["summary"], "still working")
        self.assertEqual(audit["continue_xml"], "<aize_goal_feedback />")

    def test_record_session_agent_contact_preserves_fifo_order(self) -> None:
        update_session_goal_flags(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
            agent_welcome_enabled=True,
        )
        record_session_agent_contact(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
            service_id="service-codex-001",
            provider="codex",
        )
        record_session_agent_contact(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
            service_id="service-claude-001",
            provider="claude",
        )
        record_session_agent_contact(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
            service_id="service-codex-001",
            provider="codex",
            turn_completed_at="2026-03-20T12:10:00Z",
        )

        contacts = list_session_agent_contacts(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
        )

        self.assertEqual([item["service_id"] for item in contacts], ["service-codex-001", "service-claude-001"])
        self.assertEqual(contacts[0]["last_turn_completed_at"], "2026-03-20T12:10:00Z")

    def test_session_history_is_written_to_timeline_jsonl(self) -> None:
        append_history(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
            entry={"direction": "in", "ts": "2026-03-22T19:00:00Z", "text": "hello"},
            limit=20,
        )

        timeline_path = session_timeline_path(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
        )
        self.assertTrue(timeline_path.exists())
        lines = timeline_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["text"], "hello")

    def test_goal_manager_fifo_is_session_scoped(self) -> None:
        pending = append_goal_manager_pending_input(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
            entry={"kind": "turn_completed", "ts": "2026-03-22T19:00:01Z", "service_id": "service-codex-001"},
        )

        fifo_path = session_goal_manager_pending_path(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
        )
        self.assertTrue(fifo_path.exists())
        self.assertEqual(len(pending), 1)
        loaded = load_goal_manager_pending_inputs(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
        )
        self.assertEqual(loaded[0]["kind"], "turn_completed")

    def test_session_storage_bootstraps_goal_manager_and_dag_files(self) -> None:
        goal_manager_state_path = session_goal_manager_state_path(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
        )
        self.assertTrue(goal_manager_state_path.exists())
        self.assertEqual(json.loads(goal_manager_state_path.read_text(encoding="utf-8"))["state"], "idle")
        self.assertTrue(
            session_goal_manager_reviews_path(
                self.runtime_root,
                username=TEST_USERNAME,
                session_id=self.session_id,
            ).exists()
        )
        self.assertEqual(
            json.loads(
                session_dag_parents_path(
                    self.runtime_root,
                    username=TEST_USERNAME,
                    session_id=self.session_id,
                ).read_text(encoding="utf-8")
            )["parents"],
            [],
        )
        self.assertEqual(
            json.loads(
                session_dag_children_path(
                    self.runtime_root,
                    username=TEST_USERNAME,
                    session_id=self.session_id,
                ).read_text(encoding="utf-8")
            )["children"],
            [],
        )

    def test_provider_session_binding_is_written_under_session_services_directory(self) -> None:
        save_codex_session(
            self.runtime_root,
            service_id="service-codex-001",
            provider_session_id="thread-123",
            username=TEST_USERNAME,
            session_id=self.session_id,
        )

        service_state_path = session_service_state_path(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
            service_id="service-codex-001",
        )
        self.assertTrue(service_state_path.exists())
        payload = json.loads(service_state_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["codex_session_id"], "thread-123")

    def test_goal_state_response_payload_includes_agent_welcome_toggle(self) -> None:
        talk = update_session_goal_flags(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
            agent_welcome_enabled=True,
        )
        assert talk is not None
        payload = goal_state_response_payload(talk, session_id=self.session_id, default_provider="codex")
        self.assertTrue(payload["agent_welcome_enabled"])

    def test_goal_state_response_payload_includes_session_permissions(self) -> None:
        talk = get_session_settings(self.runtime_root, username=TEST_USERNAME, session_id=self.session_id)
        assert talk is not None

        payload = goal_state_response_payload(talk, session_id=self.session_id, default_provider="codex")

        self.assertEqual(
            payload["session_permissions"],
            {
                "create_child_session": True,
                "update_goal": True,
                "send_prompt": True,
                "auto_spawn_recovery": True,
                "auto_resume": True,
            },
        )
        self.assertEqual(payload["session_ui_mode"], "standard")

    def test_goal_state_response_payload_includes_user_response_wait_fields(self) -> None:
        talk = update_session_user_response_wait(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
            active=True,
            timeout_seconds=600,
            prompt_text="Need the deployment region.",
            source_service_id="service-codex-001",
        )
        assert talk is not None

        payload = goal_state_response_payload(talk, session_id=self.session_id, default_provider="codex")

        self.assertTrue(payload["user_response_wait_active"])
        self.assertEqual(payload["user_response_wait_timeout_seconds"], 600)
        self.assertEqual(payload["user_response_wait_effective_timeout_seconds"], 300)
        self.assertEqual(payload["user_response_wait_prompt_text"], "Need the deployment region.")
        self.assertEqual(payload["user_response_wait_source_service_id"], "service-codex-001")
        self.assertTrue(payload["user_response_wait_until_at"])

    def test_consume_session_due_user_response_wait_clears_wait_state(self) -> None:
        talk = update_session_user_response_wait(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
            active=True,
            timeout_seconds=600,
            prompt_text="Need the deployment region.",
            source_service_id="service-codex-001",
        )
        assert talk is not None
        stored = get_session_settings(self.runtime_root, username=TEST_USERNAME, session_id=self.session_id)
        assert stored is not None
        stored["user_response_wait_until_at"] = "2026-03-20T12:00:00Z"
        write_json_file(
            session_metadata_path(self.runtime_root, username=TEST_USERNAME, session_id=self.session_id),
            stored,
        )

        cleared = consume_session_due_user_response_wait(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
        )

        assert cleared is not None
        self.assertFalse(cleared["user_response_wait_active"])
        self.assertEqual(cleared["user_response_wait_started_at"], talk["user_response_wait_started_at"])
        self.assertEqual(cleared["user_response_wait_timeout_seconds"], 600)
        self.assertTrue(cleared["user_response_wait_last_timeout_at"])

    def test_extract_user_response_wait_control_strips_hidden_xml(self) -> None:
        visible_text, control = _extract_user_response_wait_control(
            "Need the deployment region.\n<aize_user_response_wait><timeout_seconds>600</timeout_seconds></aize_user_response_wait>"
        )

        self.assertEqual(visible_text, "Need the deployment region.")
        self.assertEqual(control, {"timeout_seconds": 600})

    def test_completed_goal_dispatch_is_deferred_for_non_user_fifo_inputs(self) -> None:
        self.assertTrue(
            _should_defer_dispatch_for_completed_goal(
                session_settings={
                    "goal_active": True,
                    "goal_completed": True,
                    "goal_progress_state": "complete",
                },
                pending_inputs=[
                    {"kind": "goal_feedback"},
                    {"kind": "turn_completed"},
                ],
            )
        )

    def test_completed_goal_dispatch_is_not_deferred_for_user_message(self) -> None:
        self.assertFalse(
            _should_defer_dispatch_for_completed_goal(
                session_settings={
                    "goal_active": True,
                    "goal_completed": True,
                    "goal_progress_state": "complete",
                },
                pending_inputs=[
                    {"kind": "goal_feedback"},
                    {"kind": "user_message"},
                ],
            )
        )

    def test_build_prompt_mentions_user_response_wait_control(self) -> None:
        prompt = build_prompt(
            {
                "persona": "Test persona",
                "max_turns": 100,
                "response_schema_id": "service_control_v1",
            },
            {"display_name": "HttpBridge"},
            "<aize_input_batch />",
            8,
        )

        self.assertIn("aize_user_response_wait", prompt)
        self.assertIn("300", prompt)

    def test_http_handler_source_renders_wait_signal_in_nav_and_session_map(self) -> None:
        source = (SRC / "runtime" / "http_handler.py").read_text(encoding="utf-8")
        self.assertIn("talk-signal-wait", source)
        self.assertIn("Waiting User Response", source)
        self.assertIn("Wait Timed Out", source)

    def test_html_renderer_source_tracks_wait_state_in_nav_and_goal_board(self) -> None:
        source = (SRC / "runtime" / "html_renderer.py").read_text(encoding="utf-8")
        self.assertIn("userResponseWaitStatus", source)
        self.assertIn("talk-signal-wait", source)
        self.assertIn("Wait Recorded", source)
        self.assertIn("Waiting User Response", source)

    def test_pending_turn_completed_events_since_last_review_aggregates_multiple_agents(self) -> None:
        history = [
            {
                "ts": "2026-03-20T12:00:00Z",
                "event_type": "turn.completed",
                "service_id": "service-codex-001",
                "event": {"provider": "codex", "status": "success", "reply_index": 1, "completed_at": "2026-03-20T12:00:00Z"},
                "text": "codex done",
            },
            {
                "ts": "2026-03-20T12:05:00Z",
                "event_type": "turn.completed",
                "service_id": "service-claude-001",
                "event": {"provider": "claude", "status": "success", "reply_index": 2, "completed_at": "2026-03-20T12:05:00Z"},
                "text": "claude done",
            },
        ]

        pending = pending_turn_completed_events_since_last_review(
            history,
            last_reviewed_turn_completed_at="2026-03-20T12:01:00Z",
        )

        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["service_id"], "service-claude-001")

    def test_goal_followup_dispatch_targets_respects_contact_fifo(self) -> None:
        targets = goal_followup_dispatch_targets(
            [
                {"service_id": "service-claude-001"},
                {"service_id": "service-codex-001"},
            ],
            [
                {"service_id": "service-codex-001"},
                {"service_id": "service-openclaw-001"},
            ],
        )

        self.assertEqual(targets, ["service-claude-001", "service-codex-001", "service-openclaw-001"])

    def test_collect_and_verify_turn_completed_artifacts_downloads_and_validates_json(self) -> None:
        artifact_source = self.runtime_root / "sample.json"
        artifact_source.write_text(json.dumps({"ok": True}), encoding="utf-8")
        artifact_url = artifact_source.resolve().as_uri()
        from runtime.persistent_state import append_pending_input

        append_pending_input(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
            entry={
                "kind": "turn_completed",
                "role": "system",
                "text": "\n".join(
                    [
                        "<aize_turn_completed>",
                        "  <service_id>service-codex-001</service_id>",
                        "  <reply_index>1</reply_index>",
                        "  <process_id>proc-1</process_id>",
                        "  <run_id>run-1</run_id>",
                        "  <completed_at>2026-03-20T12:30:00Z</completed_at>",
                        f"  <latest_reply>Download {artifact_url}</latest_reply>",
                        "</aize_turn_completed>",
                    ]
                ),
            },
        )

        verified = collect_and_verify_turn_completed_artifacts(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
            last_reviewed_turn_completed_at="",
        )

        self.assertEqual(len(verified), 1)
        self.assertEqual(verified[0]["validation"], "json_valid")
        self.assertTrue(Path(verified[0]["local_path"]).exists())

    def test_goal_audit_followup_only_enqueues_for_in_progress_all_clear(self) -> None:
        self.assertTrue(
            goal_audit_should_enqueue_agent_followup(
                progress_state="in_progress",
                audit_state="all_clear",
            )
        )
        self.assertFalse(
            goal_audit_should_enqueue_agent_followup(
                progress_state="complete",
                audit_state="all_clear",
            )
        )
        self.assertFalse(
            goal_audit_should_enqueue_agent_followup(
                progress_state="in_progress",
                audit_state="needs_compact",
            )
        )

    def test_completed_goal_releases_session_provider(self) -> None:
        released = maybe_release_session_provider(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
            talk={
                "goal_active": True,
                "goal_completed": True,
                "goal_progress_state": "complete",
            },
        )

        self.assertIsNone(released)

    def test_module_level_function_names_are_unique(self) -> None:
        duplicates: list[tuple[str, str, list[int]]] = []
        for path in sorted((ROOT / "src").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            seen: dict[str, list[int]] = {}
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    seen.setdefault(node.name, []).append(node.lineno)
            for name, lines in seen.items():
                if len(lines) > 1:
                    duplicates.append((str(path.relative_to(ROOT)), name, lines))

        self.assertEqual([], duplicates)

    def test_persist_goal_audit_completion_stores_request_compact_fields(self) -> None:
        log_path = self.runtime_root / "logs" / "goal-manager.jsonl"
        audit = {
            "goal_audit_session_id": "session-1",
            "progress_state": "in_progress",
            "audit_state": "needs_compact",
            "goal_satisfied": False,
            "summary": "Need more work",
            "continue_xml": "<continue />",
            "request_compact": True,
            "request_compact_reason": "Repeatedly ignored guidance",
        }

        persist_goal_audit_completion(
            runtime_root=self.runtime_root,
            log_path=log_path,
            service_id="service-codex-001",
            process_id="proc-1",
            goal_audit_job_id="job-1",
            username=TEST_USERNAME,
            session_id=self.session_id,
            audit=audit,
        )

        written = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(written[0]["type"], "service.goal_audit_completed")
        self.assertEqual(written[0]["progress_state"], "in_progress")
        self.assertEqual(written[0]["audit_state"], "needs_compact")
        self.assertTrue(written[0]["request_compact"])
        self.assertEqual(written[0]["request_compact_reason"], "Repeatedly ignored guidance")
        history = get_history(self.runtime_root, username=TEST_USERNAME, session_id=self.session_id)
        self.assertEqual(history[0]["event"]["audit_state"], "needs_compact")
        self.assertEqual(history[0]["event"]["request_compact_reason"], "Repeatedly ignored guidance")

    def test_goal_auto_compact_state_round_trip_payload(self) -> None:
        from runtime.persistent_state import update_session_goal

        update_session_goal(self.runtime_root, username=TEST_USERNAME, session_id=self.session_id, goal_text="Ship it")
        talk = update_session_goal_flags(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
            goal_auto_compact_enabled=True,
            goal_reset_completed_on_prompt=False,
        )
        assert talk is not None

        stored = get_session_settings(self.runtime_root, username=TEST_USERNAME, session_id=self.session_id)
        assert stored is not None
        payload = goal_state_response_payload(stored, session_id=self.session_id, default_provider="codex")

        self.assertTrue(payload["goal_auto_compact_enabled"])
        self.assertFalse(payload["goal_reset_completed_on_prompt"])
        self.assertEqual(payload["goal_mode"], "active")
        self.assertEqual(payload["goal_progress_state"], "in_progress")
        self.assertEqual(payload["goal_audit_state"], "all_clear")
        self.assertEqual(payload["session_id"], self.session_id)

    def test_goal_updates_append_history_and_expose_active_goal_id(self) -> None:
        from runtime.persistent_state import update_session_goal

        first = update_session_goal(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
            goal_text="First goal",
        )
        assert first is not None
        second = update_session_goal(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
            goal_text="Second goal",
        )
        assert second is not None

        self.assertEqual(second["goal_text"], "Second goal")
        self.assertEqual(second["active_goal_id"], second["goal_id"])
        self.assertEqual(len(second["goal_history"]), 2)
        self.assertEqual(second["goal_history"][0]["goal_text"], "First goal")
        self.assertEqual(second["goal_history"][1]["goal_text"], "Second goal")
        self.assertEqual(second["goal_history"][1]["previous_goal_id"], first["goal_id"])

    def test_old_goal_completion_does_not_overwrite_new_active_goal(self) -> None:
        from runtime.persistent_state import update_session_goal

        first = update_session_goal(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
            goal_text="First goal",
        )
        assert first is not None
        second = update_session_goal(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
            goal_text="Second goal",
        )
        assert second is not None

        talk = update_session_goal_flags(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
            goal_id=first["goal_id"],
            goal_progress_state="complete",
        )
        assert talk is not None

        self.assertEqual(talk["goal_text"], "Second goal")
        self.assertFalse(talk["goal_completed"])
        self.assertEqual(talk["active_goal_id"], second["goal_id"])
        first_history = next(item for item in talk["goal_history"] if item["goal_id"] == first["goal_id"])
        second_history = next(item for item in talk["goal_history"] if item["goal_id"] == second["goal_id"])
        self.assertTrue(first_history["goal_completed"])
        self.assertEqual(first_history["goal_progress_state"], "complete")
        self.assertFalse(second_history["goal_completed"])

    def test_no_goal_mode_forces_inactive_state(self) -> None:
        from runtime.persistent_state import update_session_goal

        update_session_goal(self.runtime_root, username=TEST_USERNAME, session_id=self.session_id, goal_text="")
        talk = update_session_goal_flags(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
            goal_active=True,
            goal_progress_state="complete",
            goal_audit_state="panic",  # no-op: audit_state is now agent-side only
        )
        assert talk is not None

        self.assertEqual(talk["goal_mode"], "no_goal")
        self.assertFalse(talk["goal_active"])
        self.assertFalse(talk["goal_completed"])
        self.assertEqual(talk["goal_progress_state"], "in_progress")
        # goal_audit_state is agent-side; it must not appear on the session talk record
        self.assertNotIn("goal_audit_state", talk)
        # The response payload still returns all_clear as the safe default
        payload = goal_state_response_payload(talk, session_id=self.session_id, default_provider="codex")
        self.assertEqual(payload["goal_audit_state"], "all_clear")

    def test_latest_goal_manager_runtime_state_reports_running(self) -> None:
        history = [
            {"ts": "2026-03-19T09:00:00Z", "event_type": "service.goal_audit_completed", "service_id": "service-codex-001", "event": {"goal_satisfied": False}},
            {"ts": "2026-03-19T09:00:01Z", "event_type": "service.goal_audit_started", "service_id": "service-codex-002"},
        ]

        state = latest_goal_manager_runtime_state(history)

        self.assertEqual(state["state"], "running")
        self.assertEqual(state["service_id"], "service-codex-002")

    def test_build_session_runtime_summary_prefers_active_worker(self) -> None:
        talk = {
            "session_id": self.session_id,
            "session_id": self.session_id,
            "label": "Goal Talk",
            "goal_text": "Ship it",
            "goal_active": True,
            "goal_completed": False,
            "goal_progress_state": "in_progress",
            "preferred_provider": "codex",
            "service_id": "service-codex-001",
        }
        history = [
            {"ts": "2026-03-19T09:00:00Z", "event_type": "agent.turn_started", "service_id": "service-codex-002"},
            {"ts": "2026-03-19T09:00:01Z", "event_type": "service.goal_audit_started", "service_id": "service-codex-002"},
        ]

        summary = build_session_runtime_summary(
            talk,
            history_entries=history,
            codex_service_pool=["service-codex-001", "service-codex-002"],
            claude_service_pool=["service-claude-001"],
            default_provider="codex",
        )

        self.assertTrue(summary["agent_running"])
        self.assertEqual(summary["session_id"], self.session_id)
        self.assertEqual(summary["worker"]["service_id"], "service-codex-002")
        self.assertEqual(summary["worker"]["slot"], 2)
        self.assertEqual(summary["goal_manager_state"], "running")
        self.assertEqual(summary["goal_manager_provider"], "codex")
        self.assertEqual(summary["goal_manager_worker"]["slot"], 2)

    def test_goal_state_response_payload_includes_goal_manager_provider(self) -> None:
        talk = get_session_settings(self.runtime_root, username=TEST_USERNAME, session_id=self.session_id)
        assert talk is not None
        payload = goal_state_response_payload(
            talk,
            session_id=self.session_id,
            default_provider="codex",
            goal_manager_worker={"provider": "claude", "service_id": "service-claude-001", "slot": 1},
        )
        self.assertEqual(payload["goal_manager_provider"], "claude")

    def test_build_session_runtime_summary_derives_wait_status(self) -> None:
        talk = {
            "session_id": self.session_id,
            "label": "Goal Talk",
            "goal_text": "Ship it",
            "goal_active": True,
            "goal_completed": False,
            "goal_progress_state": "in_progress",
            "preferred_provider": "codex",
            "user_response_wait_active": False,
            "user_response_wait_started_at": "2026-03-23T20:00:00Z",
            "user_response_wait_last_timeout_at": "2026-03-23T20:05:00Z",
        }

        summary = build_session_runtime_summary(
            talk,
            history_entries=[],
            codex_service_pool=["service-codex-001"],
            claude_service_pool=["service-claude-001"],
            default_provider="codex",
        )

        self.assertEqual(summary["user_response_wait_status"], "timed_out")
        self.assertEqual(summary["user_response_wait_started_at"], "2026-03-23T20:00:00Z")

    def test_build_session_runtime_summary_carries_wait_prompt_text(self) -> None:
        talk = {
            "session_id": self.session_id,
            "label": "Goal Talk",
            "goal_text": "Ship it",
            "goal_active": True,
            "goal_completed": False,
            "goal_progress_state": "in_progress",
            "preferred_provider": "codex",
            "user_response_wait_active": True,
            "user_response_wait_started_at": "2026-03-23T20:00:00Z",
            "user_response_wait_prompt_text": "Need the deployment region and account ID.",
        }

        summary = build_session_runtime_summary(
            talk,
            history_entries=[],
            codex_service_pool=["service-codex-001"],
            claude_service_pool=["service-claude-001"],
            default_provider="codex",
        )

        self.assertEqual(summary["user_response_wait_prompt_text"], "Need the deployment region and account ID.")

    def test_talk_records_are_normalized_to_session_ids(self) -> None:
        stored = get_session_settings(self.runtime_root, username=TEST_USERNAME, session_id=self.session_id)
        assert stored is not None
        self.assertEqual(stored["session_id"], self.session_id)
        self.assertEqual(stored["session_id"], self.session_id)

    def test_codex_session_listing_keeps_conversation_session_id(self) -> None:
        save_codex_session(
            self.runtime_root,
            service_id="service-codex-001",
            provider_session_id="provider-session-1",
            username=TEST_USERNAME,
            session_id=self.session_id,
        )
        sessions = list_codex_sessions(self.runtime_root, service_id="service-codex-001")
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["conversation_session_id"], self.session_id)
        self.assertEqual(sessions[0]["session_id"], "provider-session-1")

    def test_create_child_conversation_session_persists_dag_edges(self) -> None:
        child = create_child_conversation_session(
            self.runtime_root,
            username=TEST_USERNAME,
            parent_session_id=self.session_id,
            label="Subgoal",
            goal_text="Do child work",
        )
        assert child is not None
        child_id = str(child["session_id"])
        self.assertEqual(list_session_children(self.runtime_root, username=TEST_USERNAME, session_id=self.session_id), [child_id])
        self.assertEqual(list_session_parents(self.runtime_root, username=TEST_USERNAME, session_id=child_id), [self.session_id])
        stored_parent = get_session_settings(self.runtime_root, username=TEST_USERNAME, session_id=self.session_id)
        assert stored_parent is not None
        self.assertTrue(stored_parent["waiting_on_children"])

    def test_create_child_conversation_session_records_origin_provenance(self) -> None:
        update_session_goal(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
            goal_text="Parent goal",
            updated_by_username=TEST_USERNAME,
            updated_by_type="user",
            origin_session_id=self.session_id,
        )
        child = create_child_conversation_session(
            self.runtime_root,
            username=TEST_USERNAME,
            parent_session_id=self.session_id,
            label="Subgoal",
            goal_text="Child goal",
            created_by_username="goalmanager",
            created_by_type="system",
        )
        assert child is not None
        self.assertEqual(child.get("created_by_username"), "goalmanager")
        self.assertEqual(child.get("created_by_type"), "system")
        self.assertEqual(child.get("origin_session_id"), self.session_id)
        self.assertEqual(child.get("origin_goal_text"), "Parent goal")
        history = child.get("goal_history")
        assert isinstance(history, list)
        self.assertEqual(history[-1].get("updated_by_username"), "goalmanager")
        self.assertEqual(history[-1].get("updated_by_type"), "system")
        self.assertEqual(history[-1].get("origin_session_id"), self.session_id)
        self.assertEqual(history[-1].get("origin_goal_text"), "Parent goal")

    def test_goalmanager_system_account_is_reserved_and_not_counted_as_bootstrap_user(self) -> None:
        self.assertFalse(auth_has_users(self.runtime_root))
        record = resolve_user_record(self.runtime_root, username="goalmanager")
        assert record is not None
        self.assertTrue(record.get("system_account"))
        self.assertTrue(record.get("login_disabled"))
        self.assertFalse(verify_user_password(self.runtime_root, username="goalmanager", password="anything"))
        ok, username = bootstrap_root_user(self.runtime_root, password="root-pass")
        self.assertTrue(ok)
        self.assertEqual(username, "root")
        self.assertTrue(auth_has_users(self.runtime_root))

    def test_default_root_session_is_migrated_to_root_label_and_creator(self) -> None:
        ok, _username = bootstrap_root_user(self.runtime_root, password="root-pass")
        self.assertTrue(ok)
        create_session(self.runtime_root, username="root")
        session = get_session_settings(self.runtime_root, username="root", session_id="default")
        assert session is not None
        self.assertEqual(session.get("label"), "Root")
        self.assertEqual(session.get("created_by_username"), "root")
        self.assertEqual(session.get("session_group"), "root")
        self.assertEqual(session_ui_mode(session), "map_only")
        self.assertTrue(session_operation_allowed(session, "create_child_session"))
        self.assertFalse(session_operation_allowed(session, "update_goal"))
        self.assertFalse(session_operation_allowed(session, "send_prompt"))

    def test_materialize_goal_child_sessions_creates_children_with_goalmanager_provenance(self) -> None:
        created = _materialize_goal_child_sessions(
            runtime_root=self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
            goal_id="goal-123",
            goal_text="Parent goal",
            goal_manager_service_id="service-codex-001",
            child_goal_requests=[
                {
                    "service_id": "service-codex-001",
                    "label": "Implement",
                    "goal_text": "Write code",
                },
                {
                    "service_id": "service-claude-001",
                    "label": "Verify",
                    "goal_text": "Review code",
                },
            ],
        )
        self.assertEqual(len(created), 2)
        children = list_session_children(self.runtime_root, username=TEST_USERNAME, session_id=self.session_id)
        self.assertEqual(len(children), 2)
        first_child = get_session_settings(self.runtime_root, username=TEST_USERNAME, session_id=children[0])
        assert first_child is not None
        self.assertEqual(first_child.get("created_by_username"), "goalmanager")
        self.assertEqual(first_child.get("origin_session_id"), self.session_id)
        self.assertEqual(first_child.get("origin_goal_id"), "goal-123")
        self.assertEqual(first_child.get("goal_history", [])[-1].get("updated_by_username"), "goalmanager")
        history = get_history(self.runtime_root, username=TEST_USERNAME, session_id=self.session_id)
        self.assertTrue(any(entry.get("event_type") == "service.goal_child_sessions_created" for entry in history))

    def test_session_goal_context_includes_root_two_and_current_two_without_duplicates(self) -> None:
        root_first = update_session_goal(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
            goal_text="Root first goal",
        )
        assert root_first is not None
        root_second = update_session_goal(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
            goal_text="Root second goal",
        )
        assert root_second is not None
        child = create_child_conversation_session(
            self.runtime_root,
            username=TEST_USERNAME,
            parent_session_id=self.session_id,
            label="Subgoal",
            goal_text="Child first goal",
        )
        assert child is not None
        child_id = str(child["session_id"])
        update_session_goal(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=child_id,
            goal_text="Child second goal",
        )

        context = session_goal_context(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=child_id,
        )

        self.assertEqual(
            [item["goal_text"] for item in context],
            [
                "Root first goal",
                "Root second goal",
                "Child first goal",
                "Child second goal",
            ],
        )

    def test_session_goal_context_uses_configurable_limits(self) -> None:
        update_session_goal(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
            goal_text="Root first goal",
        )
        update_session_goal(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
            goal_text="Root second goal",
        )
        update_session_goal(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
            goal_text="Root third goal",
        )
        child = create_child_conversation_session(
            self.runtime_root,
            username=TEST_USERNAME,
            parent_session_id=self.session_id,
            label="Subgoal",
            goal_text="Child first goal",
        )
        assert child is not None
        child_id = str(child["session_id"])
        update_session_goal_flags(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=child_id,
        )
        session = get_session_settings(self.runtime_root, username=TEST_USERNAME, session_id=child_id)
        assert session is not None
        session["goal_context_root_limit"] = 1
        session["goal_context_recent_limit"] = 1
        write_json_file(
            session_metadata_path(self.runtime_root, username=TEST_USERNAME, session_id=child_id),
            session,
        )

        context = session_goal_context(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=child_id,
        )

        self.assertEqual(
            [item["goal_text"] for item in context],
            ["Root first goal", "Child first goal"],
        )

    def test_record_session_agent_contact_keeps_single_native_when_agent_welcome_disabled(self) -> None:
        record_session_agent_contact(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
            service_id="service-codex-001",
            provider="codex",
        )
        record_session_agent_contact(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
            service_id="ws-peer-node-a",
            provider="ws_peer",
        )
        record_session_agent_contact(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
            service_id="service-claude-001",
            provider="claude",
        )

        contacts = list_session_agent_contacts(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
        )

        self.assertEqual(
            [item["service_id"] for item in contacts],
            ["ws-peer-node-a", "service-claude-001"],
        )

    def test_ws_peer_client_maps_targeted_goal_feedback_entry_to_system_input(self) -> None:
        dispatch = _remote_session_entry_to_dispatch(
            {
                "direction": "session_input",
                "kind": "goal_feedback",
                "service_id": "ws-peer-node-a",
                "to": "ws-peer-node-a",
                "text": "GoalManager requested more work",
                "pending_input_text": "<aize_goal_feedback><summary>continue</summary></aize_goal_feedback>",
            },
            peer_service_id="ws-peer-node-a",
        )

        assert dispatch is not None
        self.assertEqual(dispatch["log_type"], "ws_peer_client.goal_feedback_received")
        self.assertEqual(dispatch["pending_inputs"][0]["kind"], "goal_feedback")
        self.assertEqual(dispatch["pending_inputs"][0]["role"], "system")
        self.assertIn("continue", dispatch["pending_inputs"][0]["text"])

    def test_ws_peer_client_ignores_goal_feedback_for_other_peer(self) -> None:
        dispatch = _remote_session_entry_to_dispatch(
            {
                "direction": "session_input",
                "kind": "goal_feedback",
                "service_id": "ws-peer-node-b",
                "to": "ws-peer-node-b",
                "text": "GoalManager requested more work",
                "pending_input_text": "<aize_goal_feedback><summary>continue</summary></aize_goal_feedback>",
            },
            peer_service_id="ws-peer-node-a",
        )

        self.assertIsNone(dispatch)

    def test_add_session_child_rejects_cycle(self) -> None:
        child = create_conversation_session(self.runtime_root, username=TEST_USERNAME, label="Child")
        child_id = str(child["session_id"])
        add_session_child(
            self.runtime_root,
            username=TEST_USERNAME,
            parent_session_id=self.session_id,
            child_session_id=child_id,
        )
        with self.assertRaisesRegex(ValueError, "session_dag_cycle"):
            add_session_child(
                self.runtime_root,
                username=TEST_USERNAME,
                parent_session_id=child_id,
                child_session_id=self.session_id,
            )

    def test_complete_session_child_clears_waiting_on_children(self) -> None:
        child = create_child_conversation_session(
            self.runtime_root,
            username=TEST_USERNAME,
            parent_session_id=self.session_id,
            label="Subgoal",
            goal_text="Do child work",
        )
        assert child is not None
        child_id = str(child["session_id"])
        update_session_goal_flags(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=child_id,
            goal_completed=True,
            goal_progress_state="complete",
        )

        progress = complete_session_child(
            self.runtime_root,
            username=TEST_USERNAME,
            parent_session_id=self.session_id,
            child_session_id=child_id,
        )

        assert progress is not None
        self.assertFalse(progress["waiting_on_children"])
        stored_parent = get_session_settings(self.runtime_root, username=TEST_USERNAME, session_id=self.session_id)
        assert stored_parent is not None
        self.assertFalse(stored_parent["waiting_on_children"])

    def test_list_active_in_progress_child_sessions_ignores_inactive_children(self) -> None:
        child = create_child_conversation_session(
            self.runtime_root,
            username=TEST_USERNAME,
            parent_session_id=self.session_id,
            label="Subgoal",
            goal_text="Do child work",
        )
        assert child is not None
        child_id = str(child["session_id"])

        self.assertEqual(
            list_active_in_progress_child_sessions(
                self.runtime_root,
                username=TEST_USERNAME,
                session_id=self.session_id,
            ),
            [child_id],
        )

        update_session_goal_flags(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=child_id,
            goal_active=False,
            goal_completed=False,
            goal_progress_state="in_progress",
        )

        self.assertEqual(
            list_active_in_progress_child_sessions(
                self.runtime_root,
                username=TEST_USERNAME,
                session_id=self.session_id,
            ),
            [],
        )

    def test_lease_session_service_blocks_parent_while_child_goal_in_progress(self) -> None:
        child = create_child_conversation_session(
            self.runtime_root,
            username=TEST_USERNAME,
            parent_session_id=self.session_id,
            label="Subgoal",
            goal_text="Do child work",
        )
        assert child is not None

        leased = lease_session_service(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
            pool_service_ids=["service-codex-001"],
        )

        self.assertIsNone(leased)
        stored_parent = get_session_settings(self.runtime_root, username=TEST_USERNAME, session_id=self.session_id)
        assert stored_parent is not None
        self.assertTrue(stored_parent["waiting_on_children"])
        self.assertIsNone(stored_parent.get("service_id"))

    def test_ensure_panic_recovery_session_preserves_parent_child_linkage(self) -> None:
        recovery = ensure_panic_recovery_session(
            self.runtime_root,
            username=TEST_USERNAME,
            source_session_id=self.session_id,
            source_label="Goal Talk",
            panic_service_id="service-codex-001",
            event={"type": "service.worker_failed", "error": "boom"},
            preferred_provider="codex",
        )

        assert recovery is not None
        recovery_session_id = str(recovery["session_id"])
        self.assertNotEqual(recovery_session_id, self.session_id)
        self.assertEqual(
            list_session_children(self.runtime_root, username=TEST_USERNAME, session_id=self.session_id),
            [recovery_session_id],
        )
        self.assertEqual(
            list_session_parents(self.runtime_root, username=TEST_USERNAME, session_id=recovery_session_id),
            [self.session_id],
        )
        stored_recovery = get_session_settings(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=recovery_session_id,
        )
        assert stored_recovery is not None
        self.assertEqual(stored_recovery.get("session_group"), "error")
        self.assertEqual(stored_recovery.get("recovery_source_session_id"), self.session_id)
        self.assertEqual(stored_recovery.get("parent_session_id"), self.session_id)

    def test_ensure_state_ignores_legacy_talk_and_auth_session_keys(self) -> None:
        legacy_state_path = state_path(self.runtime_root)
        legacy_state_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_state_path.write_text(
            json.dumps(
                {
                    "users": {TEST_USERNAME: {"username": TEST_USERNAME}},
                    "sessions": {"token-hash": {"username": TEST_USERNAME, "active_session_id": self.session_id}},
                    "talks": {TEST_USERNAME: [{"session_id": self.session_id, "label": "Legacy Talk"}]},
                }
            ),
            encoding="utf-8",
        )

        state = ensure_state(self.runtime_root)

        self.assertIn("auth_sessions", state)
        self.assertIn("conversation_sessions", state)
        self.assertEqual(state["auth_sessions"], {})
        self.assertEqual(state["conversation_sessions"], {})
        persisted = json.loads(legacy_state_path.read_text(encoding="utf-8"))
        self.assertNotIn("sessions", persisted)
        self.assertNotIn("talks", persisted)

    def test_source_no_longer_reinjects_legacy_state_aliases(self) -> None:
        source = (SRC / "runtime" / "persistent_state.py").read_text(encoding="utf-8")
        self.assertNotIn('state["sessions"] = state["auth_sessions"]', source)
        self.assertNotIn('state["talks"] = state["conversation_sessions"]', source)

    def test_build_worker_count_summary_counts_running_and_replying(self) -> None:
        counts = build_worker_count_summary(
            service_snapshots=[
                {"service": {"kind": "codex", "status": "running"}, "process": {"status": "running"}},
                {"service": {"kind": "claude", "status": "running"}, "process": {"status": "running"}},
                {"service": {"kind": "codex", "status": "stopped"}, "process": {"status": "stopped"}},
            ],
            session_summaries=[
                {"agent_running": True, "worker": {"provider": "codex"}},
                {"agent_running": True, "worker": {"provider": "claude"}},
                {"agent_running": False, "worker": {"provider": "codex"}},
                {"agent_running": True, "preferred_provider": "codex"},
                {"agent_running": True, "worker": {"provider": "unknown"}, "preferred_provider": "codex"},
                {"agent_running": True, "bound_service_id": "service-claude-001"},
            ],
        )

        self.assertEqual(counts["codex"]["running"], 1)
        self.assertEqual(counts["claude"]["running"], 1)
        self.assertEqual(counts["codex"]["active_turns"], 3)
        self.assertEqual(counts["claude"]["active_turns"], 2)

    def test_release_nonrunnable_session_services_releases_stopped_bound_worker(self) -> None:
        init_registry(
            self.runtime_root,
            {
                "node_id": "node-test",
                "run_id": "run-test",
                "services": [
                    {
                        "service_id": "service-claude-001",
                        "kind": "claude",
                        "display_name": "Claude 1",
                        "persona": "test",
                        "max_turns": 100,
                    }
                ],
                "routes": [],
            },
        )
        init_lifecycle_state(self.runtime_root, node_id="node-test", run_id="run-test")
        register_process(
            self.runtime_root,
            process_id="proc-service-claude-001",
            service_id="service-claude-001",
            node_id="node-test",
            status="stopped",
            reason="exited",
        )
        update_service_process(
            self.runtime_root,
            service_id="service-claude-001",
            process_id="proc-service-claude-001",
            status="stopped",
        )
        talk = update_session_goal_flags(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
            preferred_provider="claude",
        )
        assert talk is not None
        talk["service_id"] = "service-claude-001"
        session = get_session_settings(self.runtime_root, username=TEST_USERNAME, session_id=self.session_id)
        assert session is not None
        session["service_id"] = "service-claude-001"
        write_json_file(session_metadata_path(self.runtime_root, username=TEST_USERNAME, session_id=self.session_id), session)

        released = release_nonrunnable_session_services(self.runtime_root)

        matching = [
            item for item in released
            if item["session_id"] == self.session_id and item["service_id"] == "service-claude-001"
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["reason"], "service_status:stopped")
        stored = get_session_settings(self.runtime_root, username=TEST_USERNAME, session_id=self.session_id)
        assert stored is not None
        self.assertIsNone(stored.get("service_id"))

    def test_release_nonrunnable_session_services_keeps_running_in_progress_binding(self) -> None:
        init_registry(
            self.runtime_root,
            {
                "node_id": "node-test",
                "run_id": "run-test",
                "services": [
                    {
                        "service_id": "service-claude-001",
                        "kind": "claude",
                        "display_name": "Claude 1",
                        "persona": "test",
                        "max_turns": 100,
                    }
                ],
                "routes": [],
            },
        )
        init_lifecycle_state(self.runtime_root, node_id="node-test", run_id="run-test")
        register_process(
            self.runtime_root,
            process_id="proc-service-claude-001",
            service_id="service-claude-001",
            node_id="node-test",
            status="running",
        )
        update_service_process(
            self.runtime_root,
            service_id="service-claude-001",
            process_id="proc-service-claude-001",
            status="running",
        )
        session = get_session_settings(self.runtime_root, username=TEST_USERNAME, session_id=self.session_id)
        assert session is not None
        session["service_id"] = "service-claude-001"
        session["preferred_provider"] = "claude"
        session["goal_text"] = "Ship it"
        session["goal_active"] = True
        session["goal_progress_state"] = "in_progress"
        session["goal_completed"] = False
        write_json_file(session_metadata_path(self.runtime_root, username=TEST_USERNAME, session_id=self.session_id), session)

        released = release_nonrunnable_session_services(self.runtime_root)

        self.assertEqual(released, [])
        stored = get_session_settings(self.runtime_root, username=TEST_USERNAME, session_id=self.session_id)
        assert stored is not None
        self.assertEqual(stored.get("service_id"), "service-claude-001")

    def test_release_nonrunnable_session_services_releases_parent_bound_worker_while_child_runs(self) -> None:
        init_registry(
            self.runtime_root,
            {
                "node_id": "node-test",
                "run_id": "run-test",
                "services": [
                    {
                        "service_id": "service-claude-001",
                        "kind": "claude",
                        "display_name": "Claude 1",
                        "persona": "test",
                        "max_turns": 100,
                    }
                ],
                "routes": [],
            },
        )
        init_lifecycle_state(self.runtime_root, node_id="node-test", run_id="run-test")
        register_process(
            self.runtime_root,
            process_id="proc-service-claude-001",
            service_id="service-claude-001",
            node_id="node-test",
            status="running",
        )
        update_service_process(
            self.runtime_root,
            service_id="service-claude-001",
            process_id="proc-service-claude-001",
            status="running",
        )
        session = get_session_settings(self.runtime_root, username=TEST_USERNAME, session_id=self.session_id)
        assert session is not None
        session["service_id"] = "service-claude-001"
        session["preferred_provider"] = "claude"
        session["goal_text"] = "Ship it"
        session["goal_active"] = True
        session["goal_progress_state"] = "in_progress"
        session["goal_completed"] = False
        write_json_file(session_metadata_path(self.runtime_root, username=TEST_USERNAME, session_id=self.session_id), session)
        child = create_child_conversation_session(
            self.runtime_root,
            username=TEST_USERNAME,
            parent_session_id=self.session_id,
            label="Subgoal",
            goal_text="Do child work",
        )
        assert child is not None

        released = release_nonrunnable_session_services(self.runtime_root)

        matching = [
            item for item in released
            if item["session_id"] == self.session_id and item["service_id"] == "service-claude-001"
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["reason"], "child_sessions_in_progress")
        stored = get_session_settings(self.runtime_root, username=TEST_USERNAME, session_id=self.session_id)
        assert stored is not None
        self.assertTrue(stored["waiting_on_children"])
        self.assertIsNone(stored.get("service_id"))

    def test_run_goal_audit_parses_two_axis_state(self) -> None:
        with patch(
            "runtime.cli_service_adapter.run_codex",
            return_value=(
                json.dumps(
                    {
                        "progress_state": "in_progress",
                        "audit_state": "needs_compact",
                        "summary": "Agent is stalling",
                        "continue_xml": "<aize_goal_feedback></aize_goal_feedback>",
                        "request_compact": True,
                        "request_compact_reason": "Repeated sabotage",
                    }
                ),
                [],
                "audit-session",
            ),
        ):
            audit = run_goal_audit(
                runtime_root=self.runtime_root,
                username=TEST_USERNAME,
                session_id=self.session_id,
                goal_text="Ship it",
                history_entries=[],
            )

        self.assertEqual(audit["progress_state"], "in_progress")
        self.assertEqual(audit["audit_state"], "needs_compact")
        self.assertTrue(audit["request_compact"])
        self.assertEqual(audit["continue_xml"], "")

    def test_run_goal_audit_parses_agent_directives(self) -> None:
        record_session_agent_contact(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
            service_id="service-codex-001",
            provider="codex",
        )
        with patch(
            "runtime.cli_service_adapter.run_codex",
            return_value=(
                json.dumps(
                    {
                        "progress_state": "in_progress",
                        "audit_state": "all_clear",
                        "summary": "Split work across agents",
                        "continue_xml": "<aize_goal_feedback><summary>session</summary></aize_goal_feedback>",
                        "request_compact": False,
                        "request_compact_reason": "",
                        "agent_directives": [
                            {
                                "service_id": "service-codex-001",
                                "audit_state": "all_clear",
                                "continue_xml": "<aize_goal_feedback><summary>agent</summary></aize_goal_feedback>",
                                "request_compact": False,
                                "request_compact_reason": "",
                            }
                        ],
                    }
                ),
                [],
                "audit-session",
            ),
        ):
            audit = run_goal_audit(
                runtime_root=self.runtime_root,
                username=TEST_USERNAME,
                session_id=self.session_id,
                goal_text="Ship it",
                history_entries=[],
            )

        self.assertEqual(audit["progress_state"], "in_progress")
        self.assertEqual(len(audit["agent_directives"]), 1)
        self.assertEqual(audit["agent_directives"][0]["service_id"], "service-codex-001")
        self.assertIn("<aize_goal_feedback>", audit["agent_directives"][0]["continue_xml"])

    def test_run_goal_audit_parses_child_goal_requests(self) -> None:
        with patch(
            "runtime.cli_service_adapter.run_codex",
            return_value=(
                json.dumps(
                    {
                        "progress_state": "in_progress",
                        "audit_state": "all_clear",
                        "summary": "Need a child session",
                        "continue_xml": "<aize_goal_feedback><summary>session</summary></aize_goal_feedback>",
                        "request_compact": False,
                        "request_compact_reason": "",
                        "agent_directives": [],
                        "child_goal_requests": [
                            {
                                "service_id": "service-codex-001",
                                "label": "Subgoal",
                                "goal_text": "Implement child task",
                            },
                            {
                                "service_id": "service-claude-001",
                                "label": "Subgoal",
                                "goal_text": "Verify child task",
                            }
                        ],
                    }
                ),
                [],
                "audit-session",
            ),
        ):
            audit = run_goal_audit(
                runtime_root=self.runtime_root,
                username=TEST_USERNAME,
                session_id=self.session_id,
                goal_text="Ship it",
                history_entries=[],
            )

        self.assertEqual(len(audit["child_goal_requests"]), 2)
        self.assertEqual(audit["child_goal_requests"][0]["service_id"], "service-codex-001")
        self.assertEqual(audit["child_goal_requests"][0]["label"], "Subgoal")
        self.assertEqual(audit["child_goal_requests"][0]["goal_text"], "Implement child task")
        self.assertEqual(audit["child_goal_requests"][1]["service_id"], "service-claude-001")
        self.assertEqual(audit["child_goal_requests"][1]["goal_text"], "Verify child task")

    def test_run_goal_audit_discards_single_child_goal_request(self) -> None:
        with patch(
            "runtime.cli_service_adapter.run_codex",
            return_value=(
                json.dumps(
                    {
                        "progress_state": "in_progress",
                        "audit_state": "all_clear",
                        "summary": "Only one split requested",
                        "continue_xml": "<aize_goal_feedback><summary>session</summary></aize_goal_feedback>",
                        "request_compact": False,
                        "request_compact_reason": "",
                        "agent_directives": [],
                        "child_goal_requests": [
                            {
                                "service_id": "service-codex-001",
                                "label": "Subgoal",
                                "goal_text": "Implement child task",
                            }
                        ],
                    }
                ),
                [],
                "audit-session",
            ),
        ):
            audit = run_goal_audit(
                runtime_root=self.runtime_root,
                username=TEST_USERNAME,
                session_id=self.session_id,
                goal_text="Ship it",
                history_entries=[],
            )

        self.assertEqual(audit["child_goal_requests"], [])

    def test_run_goal_audit_forwards_provider_events(self) -> None:
        seen: list[dict[str, str]] = []
        seen_schema_ids: list[str | None] = []

        def fake_run_codex(prompt: str, *, session_id: str | None, response_schema_id: str | None, on_event=None):
            seen_schema_ids.append(response_schema_id)
            if on_event is not None:
                on_event({"type": "item.started", "item": {"type": "reasoning"}})
            return (
                json.dumps(
                    {
                        "progress_state": "complete",
                        "audit_state": "all_clear",
                        "summary": "done",
                        "continue_xml": "",
                    }
                ),
                [],
                "audit-session",
            )

        with patch("runtime.cli_service_adapter.run_codex", side_effect=fake_run_codex):
            audit = run_goal_audit(
                runtime_root=self.runtime_root,
                username=TEST_USERNAME,
                session_id=self.session_id,
                goal_text="Ship it",
                history_entries=[],
                on_event=seen.append,
            )

        self.assertEqual(audit["progress_state"], "complete")
        self.assertEqual([event["type"] for event in seen], ["item.started"])
        self.assertEqual(seen_schema_ids, ["goal_audit_v1"])

    def test_run_goal_audit_prefers_session_provider_over_callsite_provider(self) -> None:
        update_session_goal_flags(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
            preferred_provider="claude",
        )
        with patch(
            "runtime.cli_service_adapter.run_claude",
            return_value=(
                json.dumps(
                    {
                        "progress_state": "complete",
                        "audit_state": "all_clear",
                        "summary": "done",
                        "continue_xml": "",
                        "request_compact": False,
                        "request_compact_reason": "",
                        "agent_directives": [],
                        "child_goal_requests": [],
                    }
                ),
                [],
                "claude-audit-session",
            ),
        ) as claude_mock, patch(
            "runtime.cli_service_adapter.run_codex"
        ) as codex_mock:
            audit = run_goal_audit(
                runtime_root=self.runtime_root,
                username=TEST_USERNAME,
                session_id=self.session_id,
                goal_text="Ship it",
                history_entries=[],
                provider_kind="",
            )

        self.assertEqual(audit["goal_audit_session_id"], "claude-audit-session")
        claude_mock.assert_called_once()
        codex_mock.assert_not_called()

    def test_run_goal_audit_retries_codex_with_goal_audit_schema(self) -> None:
        calls: list[tuple[str | None, str | None]] = []

        def fake_run_codex(prompt: str, *, session_id: str | None, response_schema_id: str | None, on_event=None):
            calls.append((session_id, response_schema_id))
            if len(calls) == 1:
                return ("not json", [], "audit-session-1")
            return (
                json.dumps(
                    {
                        "progress_state": "in_progress",
                        "audit_state": "all_clear",
                        "summary": "retry ok",
                        "continue_xml": "",
                    }
                ),
                [],
                "audit-session-1",
            )

        with patch("runtime.cli_service_adapter.run_codex", side_effect=fake_run_codex):
            audit = run_goal_audit(
                runtime_root=self.runtime_root,
                username=TEST_USERNAME,
                session_id=self.session_id,
                goal_text="Ship it",
                history_entries=[],
            )

        self.assertEqual(audit["summary"], "retry ok")
        self.assertEqual(
            calls,
            [
                (None, "goal_audit_v1"),
                ("audit-session-1", "goal_audit_v1"),
            ],
        )

    def test_run_goal_audit_uses_claude_when_requested(self) -> None:
        with patch(
            "runtime.cli_service_adapter.run_claude",
            return_value=(
                json.dumps(
                    {
                        "progress_state": "complete",
                        "audit_state": "all_clear",
                        "summary": "done",
                        "continue_xml": "",
                    }
                ),
                [],
                "claude-audit-session",
            ),
        ) as claude_mock, patch("runtime.cli_service_adapter.run_codex") as codex_mock:
            audit = run_goal_audit(
                runtime_root=self.runtime_root,
                username=TEST_USERNAME,
                session_id=self.session_id,
                goal_text="Ship it",
                history_entries=[],
                provider_kind="claude",
            )

        claude_mock.assert_called_once()
        codex_mock.assert_not_called()
        self.assertEqual(audit["goal_audit_session_id"], "claude-audit-session")

    def test_manual_compact_clears_audit_state_only_on_successful_checked_event(self) -> None:
        self.assertTrue(
            manual_compact_clears_audit_state(
                200,
                {
                    "ok": True,
                    "event": {
                        "type": "service.manual_compact_checked",
                        "compaction": "triggered",
                    },
                },
            )
        )
        self.assertTrue(
            manual_compact_clears_audit_state(
                200,
                {
                    "ok": True,
                    "event": {
                        "type": "service.manual_compact_checked",
                        "compaction": "skipped",
                    },
                },
            )
        )
        self.assertFalse(
            manual_compact_clears_audit_state(
                500,
                {
                    "ok": False,
                    "event": {
                        "type": "service.manual_compact_failed",
                        "compaction": "command_not_accepted",
                    },
                },
            )
        )

    def test_handle_goal_manager_compact_request_is_suppressed_when_toggle_off(self) -> None:
        update_session_goal_flags(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
            goal_auto_compact_enabled=False,
        )
        audit = {
            "request_compact": True,
            "request_compact_reason": "No implementation progress",
        }

        with patch("runtime.cli_service_adapter.goal_manager_compact_codex_session") as compact_mock:
            event = handle_goal_manager_compact_request(
                runtime_root=self.runtime_root,
                repo_root=ROOT,
                log_path=self.runtime_root / "logs" / "goal-manager.jsonl",
                service_id="service-codex-001",
                process_id="proc-1",
                goal_audit_job_id="job-1",
                username=TEST_USERNAME,
                session_id=self.session_id,
                audit=audit,
            )

        compact_mock.assert_not_called()
        assert event is not None
        self.assertEqual(event["type"], "service.goal_manager_compact_checked")
        self.assertEqual(event["compaction"], "suppressed_by_session_setting")
        history = get_history(self.runtime_root, username=TEST_USERNAME, session_id=self.session_id)
        self.assertEqual(history[0]["event_type"], "service.goal_manager_compact_checked")
        self.assertIn("No implementation progress", history[0]["text"])

    def test_handle_goal_manager_compact_request_calls_compactor_when_toggle_on(self) -> None:
        update_session_goal_flags(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
            goal_auto_compact_enabled=True,
        )
        audit = {
            "request_compact": True,
            "request_compact_reason": "Repeated sabotage",
        }

        with patch(
            "runtime.cli_service_adapter.goal_manager_compact_codex_session",
            return_value=(
                {
                    "type": "service.goal_manager_compact_checked",
                    "session_id": "session-2",
                    "session_id": self.session_id,
                    "left_percent": "12",
                    "used_percent": "88",
                    "compaction": "triggered",
                },
                0,
            ),
        ) as compact_mock:
            event = handle_goal_manager_compact_request(
                runtime_root=self.runtime_root,
                repo_root=ROOT,
                log_path=self.runtime_root / "logs" / "goal-manager.jsonl",
                service_id="service-codex-001",
                process_id="proc-1",
                goal_audit_job_id="job-1",
                username=TEST_USERNAME,
                session_id=self.session_id,
                audit=audit,
            )

        compact_mock.assert_called_once()
        assert event is not None
        self.assertEqual(event["compaction"], "triggered")
        self.assertEqual(event["reason"], "Repeated sabotage")
        history = get_history(self.runtime_root, username=TEST_USERNAME, session_id=self.session_id)
        event_types = [str(entry.get("event_type")) for entry in history[:2]]
        self.assertIn("service.goal_manager_compact_started", event_types)
        self.assertIn("service.goal_manager_compact_checked", event_types)
        checked_entry = next(entry for entry in history if entry.get("event_type") == "service.goal_manager_compact_checked")
        self.assertIn("Repeated sabotage", str(checked_entry.get("text", "")))

    def test_handle_goal_manager_compact_request_keeps_noninteractive_helper_skip_as_checked(self) -> None:
        update_session_goal_flags(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
            goal_auto_compact_enabled=True,
        )
        audit = {
            "request_compact": True,
            "request_compact_reason": "TurnCompleted auto-compact threshold exceeded.",
        }

        with patch(
            "runtime.cli_service_adapter.goal_manager_compact_codex_session",
            return_value=(
                {
                    "type": "service.goal_manager_compact_checked",
                    "session_id": self.session_id,
                    "left_percent": "18",
                    "used_percent": "82",
                    "command_status": "skipped",
                    "compaction": "skipped",
                    "wait_status": "helper_not_available_noninteractive",
                },
                0,
            ),
        ):
            event = handle_goal_manager_compact_request(
                runtime_root=self.runtime_root,
                repo_root=ROOT,
                log_path=self.runtime_root / "logs" / "goal-manager.jsonl",
                service_id="service-codex-001",
                process_id="proc-1",
                goal_audit_job_id="job-1",
                username=TEST_USERNAME,
                session_id=self.session_id,
                audit=audit,
            )

        assert event is not None
        self.assertEqual(event["type"], "service.goal_manager_compact_checked")
        self.assertEqual(event["compaction"], "skipped")
        history = get_history(self.runtime_root, username=TEST_USERNAME, session_id=self.session_id)
        event_types = [str(entry.get("event_type")) for entry in history[:2]]
        self.assertIn("service.goal_manager_compact_started", event_types)
        self.assertIn("service.goal_manager_compact_checked", event_types)
        state = json.loads(
            session_goal_manager_state_path(
                self.runtime_root,
                username=TEST_USERNAME,
                session_id=self.session_id,
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(state["state"], "idle")
        self.assertEqual(state["compact_event"]["type"], "service.goal_manager_compact_checked")

    def test_run_claude_emits_normalized_events_through_callback(self) -> None:
        class FakeProc:
            def __init__(self) -> None:
                self.stdout = StringIO(
                    "\n".join(
                        [
                            json.dumps(
                                {
                                    "type": "assistant",
                                    "message": {
                                        "id": "msg-1",
                                        "content": [
                                            {
                                                "type": "tool_use",
                                                "name": "StructuredOutput",
                                                "input": {"assistant_text": "ok", "spawn_requests": []},
                                            }
                                        ],
                                    },
                                }
                            ),
                            json.dumps(
                                {
                                    "type": "result",
                                    "result": '{"assistant_text":"ok","spawn_requests":[]}',
                                    "session_id": "claude-session-2",
                                }
                            ),
                        ]
                    )
                    + "\n"
                )
                self.stderr = StringIO("")

            def wait(self) -> int:
                return 0

        seen: list[dict[str, str]] = []

        with patch("runtime.providers.claude.subprocess.Popen", return_value=FakeProc()):
            final_text, events, next_session_id = run_claude(
                "prompt",
                session_id="claude-session-1",
                response_schema_id="service_control_v1",
                on_event=seen.append,
            )

        self.assertEqual(final_text, '{"assistant_text":"ok","spawn_requests":[]}')
        self.assertEqual(next_session_id, "claude-session-2")
        self.assertEqual([event["provider"] for event in events], ["claude", "claude"])
        self.assertEqual([event["provider"] for event in seen], ["claude", "claude"])
        self.assertEqual(events[0]["type"], "claude.assistant.tool_use")
        self.assertEqual(events[0]["tool_name"], "StructuredOutput")

    def test_normalize_claude_stream_event_extracts_message_shape(self) -> None:
        init_event = normalize_claude_stream_event(
            {
                "type": "system",
                "subtype": "init",
                "session_id": "claude-session-3",
            }
        )
        self.assertEqual(init_event["type"], "claude.system.init")

        tool_result_event = normalize_claude_stream_event(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "content": "Structured output provided successfully",
                        }
                    ]
                },
            }
        )
        self.assertEqual(tool_result_event["type"], "claude.user.tool_result")
        self.assertEqual(tool_result_event["tool_result"], "Structured output provided successfully")

    def test_summarize_provider_event_formats_claude_and_turn_completed_events(self) -> None:
        self.assertIn(
            "StructuredOutput",
            summarize_provider_event({"type": "claude.assistant.tool_use", "tool_name": "StructuredOutput"}),
        )
        self.assertIn(
            "Structured output provided successfully",
            summarize_provider_event(
                {
                    "type": "claude.user.tool_result",
                    "tool_result": "Structured output provided successfully",
                }
            ),
        )
        self.assertIn(
            "failed",
            summarize_provider_event({"type": "turn.completed", "status": "failed", "error": "schema mismatch"}),
        )

    def test_goal_audit_then_compact_event_sequence_is_logged(self) -> None:
        update_session_goal_flags(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
            goal_auto_compact_enabled=True,
        )
        log_path = self.runtime_root / "logs" / "goal-manager.jsonl"
        audit = {
            "goal_audit_session_id": "session-1",
            "progress_state": "in_progress",
            "audit_state": "needs_compact",
            "goal_satisfied": False,
            "summary": "Still incomplete",
            "continue_xml": "<continue />",
            "request_compact": True,
            "request_compact_reason": "Repeated sabotage",
        }
        persist_goal_audit_completion(
            runtime_root=self.runtime_root,
            log_path=log_path,
            service_id="service-codex-001",
            process_id="proc-1",
            goal_audit_job_id="job-1",
            username=TEST_USERNAME,
            session_id=self.session_id,
            audit=audit,
        )
        with patch(
            "runtime.cli_service_adapter.goal_manager_compact_codex_session",
            return_value=(
                {
                    "type": "service.goal_manager_compact_checked",
                    "session_id": "session-2",
                    "session_id": self.session_id,
                    "left_percent": "12",
                    "used_percent": "88",
                    "compaction": "triggered",
                },
                0,
            ),
        ):
            handle_goal_manager_compact_request(
                runtime_root=self.runtime_root,
                repo_root=ROOT,
                log_path=log_path,
                service_id="service-codex-001",
                process_id="proc-1",
                goal_audit_job_id="job-1",
                username=TEST_USERNAME,
                session_id=self.session_id,
                audit=audit,
            )

        written = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(written[0]["type"], "service.goal_audit_completed")
        self.assertEqual(written[1]["type"], "service.goal_manager_compact_started")
        self.assertEqual(written[2]["type"], "service.goal_manager_compact_checked")

    def test_dispatch_pending_visible_turn_gate_distinguishes_synthetic_followups(self) -> None:
        visible_message = {
            "type": "dispatch_pending",
            "payload": {"reason": "http_prompt"},
        }
        visible_batch = (
            "<aize_input_batch><inputs>"
            '<input index="1" kind="user_message"><role>user</role><text>hello</text></input>'
            "</inputs></aize_input_batch>"
        )
        self.assertTrue(dispatch_pending_opens_visible_turn(visible_message, visible_batch))

        restart_message = {
            "type": "dispatch_pending",
            "payload": {"reason": "restart_resume"},
        }
        restart_batch = (
            "<aize_input_batch><inputs>"
            '<input index="1" kind="restart_resume"><role>system</role><text>resume</text></input>'
            "</inputs></aize_input_batch>"
        )
        self.assertTrue(dispatch_pending_opens_visible_turn(restart_message, restart_batch))

        goal_feedback_message = {
            "type": "dispatch_pending",
            "payload": {"reason": "goal_feedback"},
        }
        goal_feedback_batch = (
            "<aize_input_batch><inputs>"
            '<input index="1" kind="goal_feedback"><role>system</role><text>continue</text></input>'
            "</inputs></aize_input_batch>"
        )
        self.assertFalse(dispatch_pending_opens_visible_turn(goal_feedback_message, goal_feedback_batch))

        turn_completed_message = {
            "type": "dispatch_pending",
            "payload": {"reason": "turn_completed"},
        }
        turn_completed_batch = (
            "<aize_input_batch><inputs>"
            '<input index="1" kind="turn_completed"><role>system</role><text>done</text></input>'
            "</inputs></aize_input_batch>"
        )
        self.assertFalse(dispatch_pending_opens_visible_turn(turn_completed_message, turn_completed_batch))

        child_completed_message = {
            "type": "dispatch_pending",
            "payload": {"reason": "child_session_completed"},
        }
        child_completed_batch = (
            "<aize_input_batch><inputs>"
            '<input index="1" kind="child_session_completed"><role>system</role><text>{}</text></input>'
            "</inputs></aize_input_batch>"
        )
        self.assertFalse(dispatch_pending_opens_visible_turn(child_completed_message, child_completed_batch))

        goal_manager_review_message = {
            "type": "dispatch_pending",
            "payload": {"reason": "goal_manager_review"},
        }
        goal_manager_review_batch = (
            "<aize_input_batch><inputs>"
            '<input index="1" kind="goal_manager_review"><role>system</role><text>{}</text></input>'
            "</inputs></aize_input_batch>"
        )
        self.assertFalse(dispatch_pending_opens_visible_turn(goal_manager_review_message, goal_manager_review_batch))

    def test_active_agent_turn_state_detects_open_turn(self) -> None:
        history = [
            {"direction": "event", "ts": "2026-03-19T10:00:00Z", "service_id": "service-codex-001", "event_type": "agent.turn_started"},
            {"direction": "event", "ts": "2026-03-19T10:00:01Z", "service_id": "service-codex-001", "event_type": "item.completed"},
        ]
        active = active_agent_turn_state(history)
        assert active is not None
        self.assertEqual(active["service_id"], "service-codex-001")

        closed = active_agent_turn_state(
            history + [{"direction": "event", "ts": "2026-03-19T10:00:02Z", "service_id": "service-codex-001", "event_type": "turn.completed"}]
        )
        self.assertIsNone(closed)

    def test_pending_progress_inquiry_exists_matches_service(self) -> None:
        pending = [
            {
                "kind": "progress_inquiry",
                "text": build_progress_inquiry_xml(
                    service_id="service-codex-001",
                    source_kind="user_message",
                    source_text="status?",
                ),
            }
        ]
        self.assertTrue(pending_progress_inquiry_exists(pending, service_id="service-codex-001"))
        self.assertFalse(pending_progress_inquiry_exists(pending, service_id="service-claude-001"))

    def test_maybe_enqueue_mid_turn_progress_inquiry_records_fallback(self) -> None:
        append_history(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
            entry={
                "direction": "event",
                "ts": "2026-03-19T10:00:00Z",
                "service_id": "service-codex-001",
                "event_type": "agent.turn_started",
                "text": "started",
            },
            limit=100,
        )

        enqueued = maybe_enqueue_mid_turn_progress_inquiry(
            runtime_root=self.runtime_root,
            log_path=self.runtime_root / "logs" / "service-http-001.jsonl",
            http_service_id="service-http-001",
            process_id="proc-http-1",
            username=TEST_USERNAME,
            session_id=self.session_id,
            source_kind="user_message",
            source_text="Where are you now?",
            provider="codex",
        )

        self.assertTrue(enqueued)
        pending = load_pending_inputs(self.runtime_root, username=TEST_USERNAME, session_id=self.session_id)
        self.assertEqual([item["kind"] for item in pending], ["progress_inquiry"])
        self.assertIn("Where are you now?", pending[0]["text"])

        history = get_history(self.runtime_root, username=TEST_USERNAME, session_id=self.session_id)
        event_types = [str(entry.get("event_type")) for entry in history]
        self.assertIn("service.progress_inquiry_requested", event_types)
        self.assertIn("service.progress_inquiry_deferred", event_types)

        # A second enqueue attempt during the same turn should be suppressed.
        second = maybe_enqueue_mid_turn_progress_inquiry(
            runtime_root=self.runtime_root,
            log_path=self.runtime_root / "logs" / "service-http-001.jsonl",
            http_service_id="service-http-001",
            process_id="proc-http-1",
            username=TEST_USERNAME,
            session_id=self.session_id,
            source_kind="user_message",
            source_text="And now?",
            provider="codex",
        )
        self.assertFalse(second)

    def test_ui_source_mentions_goal_auto_compact_toggle(self) -> None:
        source = (SRC / "runtime" / "cli_service_adapter.py").read_text(encoding="utf-8")
        self.assertIn("goal-auto-compact-toggle", source)
        self.assertIn("GoalManager autonomous compact", source)
        self.assertIn("goal_auto_compact_enabled", source)

    def test_agent_service_source_mentions_goal_manager_native_dispatch_helper(self) -> None:
        source = (SRC / "runtime" / "agent_service.py").read_text(encoding="utf-8")
        self.assertIn("def resolve_goal_manager_dispatch_service(", source)
        self.assertIn('session_settings.get("preferred_provider")', source)
        self.assertIn("return lease_session_service(", source)

    def test_agent_service_source_mentions_explicit_goal_followup_targets(self) -> None:
        source = (SRC / "runtime" / "agent_service.py").read_text(encoding="utf-8")
        self.assertIn("explicit_followup_targets", source)
        self.assertIn("queued_target_counts", source)
        self.assertIn("pending_for_target_count", source)

    def test_agent_service_source_mentions_goal_manager_service_selection_at_review_start(self) -> None:
        source = (SRC / "runtime" / "agent_service.py").read_text(encoding="utf-8")
        self.assertIn("goal_manager_service_id = resolve_goal_manager_dispatch_service(", source)
        self.assertIn('"service_id": goal_manager_service_id', source)

    def test_agent_service_source_mentions_goal_manager_review_fifo_dispatch(self) -> None:
        source = (SRC / "runtime" / "agent_service.py").read_text(encoding="utf-8")
        self.assertIn('kind="goal_manager_review"', source)
        self.assertIn('reason="goal_manager_review"', source)
        self.assertIn("run_goal_manager_review(", source)
        self.assertIn("record_session_agent_contact(", source)

    def test_maybe_resume_after_restart_requeues_latest_goal_feedback(self) -> None:
        update_session_goal(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
            goal_text="Ship it",
        )
        save_codex_session(
            self.runtime_root,
            service_id="service-codex-001",
            provider_session_id="provider-session-1",
            username=TEST_USERNAME,
            session_id=self.session_id,
        )
        update_session_goal_flags(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
            preferred_provider="codex",
        )
        write_json_file(
            session_goal_manager_reviews_path(
                self.runtime_root,
                username=TEST_USERNAME,
                session_id=self.session_id,
            ),
            {},
        )
        reviews_path = session_goal_manager_reviews_path(
            self.runtime_root,
            username=TEST_USERNAME,
            session_id=self.session_id,
        )
        reviews_path.write_text(
            json.dumps(
                {
                    "progress_state": "in_progress",
                    "audit_state": "all_clear",
                    "continue_xml": "<aize_goal_feedback><summary>resume work</summary></aize_goal_feedback>",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        class _Router:
            def __init__(self) -> None:
                self.writes: list[bytes] = []

            def write(self, data: bytes) -> None:
                self.writes.append(data)

        router = _Router()
        maybe_resume_after_restart(
            runtime_root=self.runtime_root,
            manifest={"node_id": "node-test"},
            self_service={"config": {"restart_resume": {"previous_status": "running", "previous_process_id": "proc-old"}}},
            process_id="proc-new",
            log_path=self.runtime_root / "logs" / "service-codex-001.jsonl",
            service_id="service-codex-001",
            router_conn=router,
            service_kind="codex",
        )

        pending = load_pending_inputs(self.runtime_root, username=TEST_USERNAME, session_id=self.session_id)
        self.assertEqual([item["kind"] for item in pending], ["goal_feedback"])
        self.assertIn("resume work", pending[0]["text"])
        self.assertEqual(len(router.writes), 1)

    def test_ui_source_mentions_agent_status_and_turn_cluster(self) -> None:
        source = (SRC / "runtime" / "cli_service_adapter.py").read_text(encoding="utf-8")
        self.assertIn("Agent Status", source)
        self.assertIn("agent-status-value", source)
        self.assertIn("agent-popover", source)
        self.assertIn("turn-cluster-log", source)
        self.assertIn("turn-cluster-inline-status", source)
        self.assertIn("% left", source)
        self.assertIn("audit ${auditStateLabel(goalAuditState)}", source)
        self.assertIn("buildRenderableTimeline", source)
        self.assertIn("JSON.stringify(eventEntry.event, null, 2)", source)
        self.assertIn("goal_manager_cluster", source)
        self.assertIn("GoalManager Review", source)

    def test_restart_resume_source_mentions_dangling_goal_audit_recovery(self) -> None:
        source = (SRC / "runtime" / "cli_service_adapter.py").read_text(encoding="utf-8")
        self.assertIn("has_dangling_goal_audit", source)
        self.assertIn("dangling_goal_audit", source)

    def test_source_shows_goal_feedback_uses_pending_fifo_then_dispatch(self) -> None:
        source = (SRC / "runtime" / "cli_service_adapter.py").read_text(encoding="utf-8")
        enqueue_idx = source.index('kind="goal_feedback"')
        dispatch_idx = source.index("goal_message = make_dispatch_pending_message")
        self.assertLess(enqueue_idx, dispatch_idx)
        self.assertIn("append_pending_input(", source)
        self.assertIn('message_type="dispatch_pending"', source)
        self.assertIn("goal_audit_should_enqueue_agent_followup(", source)

    def test_agent_service_source_shows_ws_peer_goal_feedback_history_transport(self) -> None:
        source = (SRC / "runtime" / "agent_service.py").read_text(encoding="utf-8")
        self.assertIn('directive_service_id.startswith("ws-peer-")', source)
        self.assertIn('"pending_input_text"', source)
        self.assertIn('"service.goal_audit_ws_peer_dispatch"', source)

    def test_source_mentions_child_session_broadcast_inputs(self) -> None:
        source = (SRC / "runtime" / "agent_service.py").read_text(encoding="utf-8")
        self.assertIn('kind="child_session_created"', source)
        self.assertIn('kind="child_session_completed"', source)
        self.assertIn("_child_session_broadcast_json(", source)

    def test_source_uses_recovery_source_session_id_for_recovery_resume(self) -> None:
        source = (SRC / "runtime" / "agent_service.py").read_text(encoding="utf-8")
        self.assertIn("recovery_source_session_id", source)
        self.assertIn("source_session_id", source)

    def test_panic_recovery_source_uses_top_level_session_creation(self) -> None:
        source = (SRC / "runtime" / "panic_recovery.py").read_text(encoding="utf-8")
        self.assertIn("create_conversation_session(", source)
        self.assertNotIn("create_child_conversation_session(", source)

    def test_source_shows_goal_manager_and_agent_clusters_share_renderer(self) -> None:
        source = (SRC / "runtime" / "cli_service_adapter.py").read_text(encoding="utf-8")
        self.assertIn("entry?.kind === 'turn_cluster' || entry?.kind === 'goal_manager_cluster'", source)
        self.assertIn("renderTurnCluster(entry)", source)
        self.assertIn("GoalManager Review", source)

    def test_source_shows_manual_compact_can_clear_panic_ui_state(self) -> None:
        source = (SRC / "runtime" / "cli_service_adapter.py").read_text(encoding="utf-8")
        self.assertIn("manual_compact_clears_audit_state", source)
        self.assertIn('response["goal_audit_state"] = "all_clear"', source)
        self.assertIn("if (payload.goal_audit_state) goalAuditState =", source)

    def test_source_shows_agent_button_and_goal_manager_compact_split(self) -> None:
        source = (SRC / "runtime" / "cli_service_adapter.py").read_text(encoding="utf-8")
        self.assertIn("controlsButton.dataset.agentControlsButton = '1';", source)
        self.assertIn("event.stopPropagation()", source)
        self.assertIn("eventType.startsWith('service.goal_manager_compact_')) return false;", source)
        self.assertIn("deriveContextStatusForService(cluster.serviceId)", source)

    def test_source_does_not_open_turn_cluster_for_standalone_status_events(self) -> None:
        source = (SRC / "runtime" / "cli_service_adapter.py").read_text(encoding="utf-8")
        self.assertIn("if (entry.direction === 'in') {", source)
        self.assertIn("timeline.push(entry);", source)
        self.assertIn("continue;", source)

    def test_source_gates_turn_started_for_synthetic_dispatch_pending(self) -> None:
        source = (SRC / "runtime" / "cli_service_adapter.py").read_text(encoding="utf-8")
        self.assertIn("dispatch_pending_opens_visible_turn(message, incoming_text)", source)
        self.assertIn('reason not in {"goal_feedback", "turn_completed"}', source)

    def test_source_shows_redispatch_uses_provider_pool_resolution(self) -> None:
        source = (SRC / "runtime" / "cli_service_adapter.py").read_text(encoding="utf-8")
        self.assertIn("preferred_provider", source)
        self.assertIn("provider_pool = codex_service_pool if preferred_provider == \"codex\" else claude_service_pool", source)
        self.assertIn("return lease_session_service(", source)
        self.assertIn("to_service = resolve_session_service_for_dispatch", source)

    def test_source_goal_update_payload_includes_previous_goal(self) -> None:
        source = (SRC / "runtime" / "cli_service_adapter.py").read_text(encoding="utf-8")
        # enqueue_goal_dispatch accepts previous_goal_text parameter
        self.assertIn("previous_goal_text: str | None = None", source)
        self.assertIn("previous_goal_id: str | None = None", source)
        # payload builds <previous_goal> element when provided
        self.assertIn("<previous_goal>", source)
        self.assertIn("<goal_id>", source)
        self.assertIn("<previous_goal_id>", source)

    def test_source_exposes_session_first_management_routes(self) -> None:
        source = (SRC / "runtime" / "cli_service_adapter.py").read_text(encoding="utf-8")
        self.assertIn('if path == "/sessions"', source)
        self.assertIn('if self.path == "/sessions"', source)
        self.assertIn('if self.path == "/session/select"', source)
        self.assertIn('if self.path == "/session/goal/state"', source)
        self.assertNotIn('"/talks"', source)
        self.assertNotIn('"/talk/select"', source)
        self.assertNotIn('"/talk/goal/state"', source)
        self.assertIn('"active_session_id": context["session_id"]', source)
        self.assertIn("html.escape(previous_goal_text)", source)

    def test_source_goal_http_handler_captures_previous_goal_before_update(self) -> None:
        source = (SRC / "runtime" / "cli_service_adapter.py").read_text(encoding="utf-8")
        # HTTP handler reads old goal_text before calling update_session_goal
        self.assertIn('previous_goal = str(old_talk.get("goal_text", "")).strip()', source)
        self.assertIn('previous_goal_id = str(old_talk.get("active_goal_id") or old_talk.get("goal_id") or "").strip() or None', source)
        # passes it to enqueue_goal_dispatch
        self.assertIn("previous_goal_text=previous_goal,", source)
        self.assertIn("previous_goal_id=previous_goal_id,", source)

    def test_source_goal_update_dispatch_still_fires_after_goal_save(self) -> None:
        source = (SRC / "runtime" / "cli_service_adapter.py").read_text(encoding="utf-8")
        # Dispatch is still triggered after goal update (goal_saved reason preserved)
        self.assertIn('reason="goal_saved"', source)
        # enqueue_goal_dispatch call site passes previous_goal_text
        prev_idx = source.index("previous_goal_text=previous_goal,")
        dispatch_idx = source.index('reason="goal_saved"')
        # previous_goal capture comes before or within same dispatch call
        self.assertLess(dispatch_idx, prev_idx + 200)

    def test_httpbridge_source_keeps_session_map_title_and_layout_stable(self) -> None:
        source = (SRC / "runtime" / "cli_service_adapter.py").read_text(encoding="utf-8")
        self.assertIn("renderPageTitle", source)
        self.assertIn("sessionMapOpen ? 'Sessions' : talkLabel", source)
        self.assertIn("sessionMapSnapshotTalkIds", source)
        self.assertIn("captureSessionMapSnapshot", source)
        self.assertIn("visibleTalkSummaries = sessionMapOpen && sessionMapSnapshotTalkIds.length", source)

    def test_httpbridge_source_preserves_session_map_scroll_on_refresh(self) -> None:
        source = (SRC / "runtime" / "cli_service_adapter.py").read_text(encoding="utf-8")
        self.assertIn("captureElementScrollState(goalBoardGrid)", source)
        self.assertIn("restoreElementScrollPosition(goalBoardGrid, scrollState)", source)
        self.assertIn("const captureElementScrollState = (element) => element ? ({", source)
        self.assertIn("const restoreElementScrollPosition = (element, state) => {", source)

    def test_httpbridge_sources_render_goal_history_without_child_session_create_ui(self) -> None:
        handler_source = (SRC / "runtime" / "http_handler.py").read_text(encoding="utf-8")
        renderer_source = (SRC / "runtime" / "html_renderer.py").read_text(encoding="utf-8")
        self.assertIn("create_child_conversation_session(", handler_source)
        self.assertIn('session_operation_allowed(talk, "update_goal")', handler_source)
        self.assertIn('session_operation_allowed(talk, "send_prompt")', handler_source)
        self.assertIn("parent_session_id = str(payload.get(\"parent_session_id\") or context[\"session_id\"] or \"\").strip()", handler_source)
        self.assertIn("updated_by_username=context[\"username\"]", handler_source)
        self.assertIn("goal-history-list", renderer_source)
        self.assertIn("renderGoalHistory", renderer_source)
        self.assertIn("renderSessionCapabilityState", renderer_source)
        self.assertNotIn("Create Child Session", renderer_source)
        self.assertNotIn("goal-board-create-form", renderer_source)
        self.assertNotIn("session-toolbar-create-form", renderer_source)
        self.assertNotIn("session-create-form", renderer_source)

    def test_goal_manager_source_creates_child_sessions_from_audit_requests(self) -> None:
        source = (SRC / "runtime" / "agent_service.py").read_text(encoding="utf-8")
        self.assertIn('audit.get("child_goal_requests")', source)
        self.assertIn("created_by_username=GOAL_MANAGER_USERNAME", source)
        self.assertIn('event_type": "service.goal_child_sessions_created"', source)

    def test_httpbridge_source_opens_session_map_when_no_session_is_requested(self) -> None:
        source = (SRC / "runtime" / "cli_service_adapter.py").read_text(encoding="utf-8")
        self.assertIn("initial_session_map_open = requested_session_id(self, query=query) is None", source)
        self.assertIn('f"let sessionMapOpen = {json.dumps(initial_session_map_open)};"', source)
        self.assertIn("setSessionMapOpen(sessionMapOpen);", source)

    def test_httpbridge_session_map_source_includes_and_filters(self) -> None:
        source = (SRC / "runtime" / "html_renderer.py").read_text(encoding="utf-8")
        self.assertIn("goal-board-filter-active", source)
        self.assertIn("goal-board-filter-in-progress", source)
        self.assertIn("goal-board-filter-awaiting-user", source)
        self.assertIn("goal-board-filter-auto-schedule", source)
        self.assertIn("if (sessionMapFilters.autoScheduleOnly && !summary?.auto_resume_enabled) return false;", source)
        self.assertIn("const waitingWithoutUserReply = Boolean(summary?.user_response_wait_active) || waitStatus === 'timed_out';", source)
        self.assertIn("const scopedList = sessionTreeSessions(baseList, sessionMapRootSessionId());", source)
        self.assertIn("return scopedList.filter(sessionMatchesMapFilters);", source)
        self.assertIn("goal-board-scope-session", source)
        self.assertIn("goal-board-scope-all", source)

    def test_httpbridge_settings_popover_and_agent_priority_ui_match_goal_shell(self) -> None:
        source = (SRC / "runtime" / "html_renderer.py").read_text(encoding="utf-8")
        self.assertIn("session-toolbar-actions", source)
        self.assertIn("setSettingsPopoverOpen(false);", source)
        self.assertIn("settingsPopoverCard || settingsPopover", source)
        self.assertIn("data-priority-move='up'", source)
        self.assertIn("row.draggable = true;", source)

    def test_httpbridge_session_panels_include_sessions_talk_toggle(self) -> None:
        source = (SRC / "runtime" / "html_renderer.py").read_text(encoding="utf-8")
        self.assertIn("id='view-session-map'", source)
        self.assertIn("viewSessionMapButton.textContent = sessionMapOpen ? 'Talk' : 'Sessions';", source)
        self.assertIn("viewSessionMapButton.setAttribute('aria-pressed', sessionMapOpen ? 'true' : 'false');", source)
        self.assertIn("viewSessionMapButton.onclick = (event) => { event.preventDefault(); toggleSessionMap(); };", source)

    def test_httpbridge_session_title_rename_defers_during_ime_composition(self) -> None:
        source = (SRC / "runtime" / "html_renderer.py").read_text(encoding="utf-8")
        self.assertIn("let sessionNameIsComposing = false;", source)
        self.assertIn("sessionNameTextarea.addEventListener('compositionstart', () => { sessionNameIsComposing = true; clearTimeout(_renameDebounce); });", source)
        self.assertIn("sessionNameTextarea.addEventListener('compositionend', () => { sessionNameIsComposing = false;", source)
        self.assertIn("if (sessionNameIsComposing) return;", source)
        self.assertIn("if (sessionNameIsComposing || ev.isComposing || ev.keyCode === 229) return;", source)

    def test_httpbridge_session_map_only_mode_hides_talk_toggle_and_forces_map_view(self) -> None:
        renderer_source = (SRC / "runtime" / "html_renderer.py").read_text(encoding="utf-8")
        handler_source = (SRC / "runtime" / "http_handler.py").read_text(encoding="utf-8")
        goal_persist_source = (SRC / "runtime" / "goal_persist.py").read_text(encoding="utf-8")
        self.assertIn("const sessionUsesMapOnlyUI = () => String(sessionUiMode || 'standard').trim().toLowerCase() === 'map_only';", renderer_source)
        self.assertIn("viewSessionMapButton.classList.toggle('is-hidden', mapOnly || accountRegisterOpen);", renderer_source)
        self.assertIn("const nextOpen = sessionUsesMapOnlyUI() ? true : Boolean(open);", renderer_source)
        self.assertIn("initial_session_map_open = bool(initial_session_map_open or initial_session_ui_mode == \"map_only\")", handler_source)
        self.assertIn('"session_ui_mode": session_ui_mode(talk),', goal_persist_source)

    def test_httpbridge_session_switch_reloads_session_ui_mode_before_toggling_map(self) -> None:
        renderer_source = (SRC / "runtime" / "html_renderer.py").read_text(encoding="utf-8")
        self.assertIn("if (Array.isArray(payload?.welcomed_agents)) welcomedAgents = payload.welcomed_agents;", renderer_source)
        self.assertIn("if (Array.isArray(payload?.selected_agents)) selectedAgents = payload.selected_agents;", renderer_source)
        self.assertIn("renderActiveAgentsSelection();", renderer_source)
        self.assertIn("const goalRes = await fetch(`/session/goal/state?session_id=${encodeURIComponent(sid)}&_=${Date.now()}`, { cache: 'no-store' });", renderer_source)
        self.assertIn("applyGoalPayload(goalPayload);", renderer_source)
        self.assertIn("setSessionMapOpen(sessionUsesMapOnlyUI());", renderer_source)
        self.assertIn("history.pushState(null, '', sessionUsesMapOnlyUI() ? sessionPathFor('') : sessionPathFor(sid));", renderer_source)

    def test_httpbridge_session_map_activity_labels_reflect_replying_and_goal_manager_review(self) -> None:
        renderer_source = (SRC / "runtime" / "html_renderer.py").read_text(encoding="utf-8")
        self.assertIn("if (String(summary?.goal_manager_state || '').trim() === 'running') return 'Reviewing';", renderer_source)
        self.assertIn("if (summary?.agent_running) return 'Replying';", renderer_source)
        self.assertIn("const goalBoardActivityBadgeClass = (summary) => {", renderer_source)
        self.assertIn("const workerProvider = ['codex', 'claude'].includes(String(worker?.provider || '').trim().toLowerCase()) ? String(worker.provider).trim().toLowerCase() : String(summary?.preferred_provider || 'codex');", renderer_source)

    def test_httpbridge_prompt_input_records_submitter_and_rejects_non_owner(self) -> None:
        source = (SRC / "runtime" / "http_handler.py").read_text(encoding="utf-8")
        self.assertIn('session_owner_username = str(talk.get("username") or "").strip()', source)
        self.assertIn('self._json(403, {"error": "session_owner_required"})', source)
        self.assertIn('"submitted_by_username": username,', source)
        self.assertIn("submitted_by_username=username,", source)

    def test_make_aize_pending_input_and_batch_xml_preserve_submitter(self) -> None:
        entry = make_aize_pending_input(
            kind="user_message",
            role="user",
            text="hello",
            submitted_by_username="alice",
            date="2026-03-26T03:21:56Z",
        )
        self.assertEqual(entry.get("submitted_by_username"), "alice")
        batch_xml = build_aize_input_batch_xml(
            sender_display_name="HttpBridge",
            username="owner",
            session_id="sess-1",
            inputs=[entry],
            instruction="continue",
        )
        self.assertIn('submitted_by_username="alice"', batch_xml)

    def test_httpbridge_source_keeps_timeline_chronological_when_not_showing_all(self) -> None:
        source = (SRC / "runtime" / "cli_service_adapter.py").read_text(encoding="utf-8")
        self.assertIn("return currentFilter === 'all' ? timeline.reverse() : timeline;", source)
        self.assertIn("const visible = currentFilter === 'all' ? timeline.slice(0, recentMessagesLimit) : timeline.slice(-recentMessagesLimit);", source)

    def test_httpbridge_source_closes_event_logs_by_default_outside_all_view(self) -> None:
        source = (SRC / "runtime" / "cli_service_adapter.py").read_text(encoding="utf-8")
        self.assertIn("const eventsShell = document.createElement('details');", source)
        self.assertIn("if (currentFilter === 'all') eventsShell.open = true;", source)
        self.assertIn("eventsTitle.textContent = currentFilter === 'all' ? 'Event Log' : 'Event Log (closed by default)';", source)

    def test_httpbridge_source_uses_timeline_all_filter_labels(self) -> None:
        source = (SRC / "runtime" / "cli_service_adapter.py").read_text(encoding="utf-8")
        self.assertIn("data-filter='messages'>Timeline</button>", source)
        self.assertIn("data-filter='all'>ALL</button>", source)
        self.assertNotIn("data-filter='messages'>In/Out</button>", source)


if __name__ == "__main__":
    unittest.main()
