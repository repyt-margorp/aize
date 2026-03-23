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
from kernel.registry import init_registry, update_service_process  # noqa: E402
from runtime.providers.claude import normalize_claude_stream_event, run_claude  # noqa: E402
from runtime.persistent_state import (  # noqa: E402
    add_session_child,
    append_history,
    append_goal_manager_pending_input,
    complete_session_child,
    create_conversation_session,
    create_child_conversation_session,
    ensure_state,
    get_history,
    list_session_agent_contacts,
    list_session_children,
    list_session_parents,
    get_session_settings,
    load_goal_manager_pending_inputs,
    list_codex_sessions,
    load_pending_inputs,
    record_session_agent_contact,
    release_nonrunnable_session_services,
    save_codex_session,
    session_dag_children_path,
    session_dag_parents_path,
    session_goal_manager_reviews_path,
    session_goal_manager_state_path,
    session_metadata_path,
    update_session_goal_flags,
    write_json_file,
)


TEST_USERNAME = "test-user"


class GoalManagerCompactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.runtime_root = Path(self.tempdir.name)
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
            history_text="recent history",
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
        )

        self.assertIn("multiple agents mixed together", prompt)
        self.assertIn("Aggregate every TurnCompleted", prompt)
        self.assertIn("Session-level additional-agent welcome signal: enabled.", prompt)
        self.assertIn('"service_id": "service-codex-001"', prompt)
        self.assertIn("Verified artifact results", prompt)
        self.assertIn("agent_directives", prompt)

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

        timeline_path = self.runtime_root.parent / ".aize-state" / "sessions" / TEST_USERNAME / self.session_id / "timeline.jsonl"
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

        fifo_path = (
            self.runtime_root.parent
            / ".aize-state"
            / "sessions"
            / TEST_USERNAME
            / self.session_id
            / "pending"
            / "goal_manager.jsonl"
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

        service_state_path = (
            self.runtime_root.parent
            / ".aize-state"
            / "sessions"
            / TEST_USERNAME
            / self.session_id
            / "services"
            / "service-codex-001.json"
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
        self.assertEqual(summary["goal_manager_worker"]["slot"], 2)

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

    def test_ensure_state_ignores_legacy_talk_and_auth_session_keys(self) -> None:
        legacy_state_path = self.runtime_root.parent / ".aize-state" / "persistent.json"
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
            ],
        )

        self.assertEqual(counts["codex"]["running"], 1)
        self.assertEqual(counts["claude"]["running"], 1)
        self.assertEqual(counts["codex"]["active_turns"], 1)
        self.assertEqual(counts["claude"]["active_turns"], 1)

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

        self.assertEqual(len(audit["child_goal_requests"]), 1)
        self.assertEqual(audit["child_goal_requests"][0]["service_id"], "service-codex-001")
        self.assertEqual(audit["child_goal_requests"][0]["label"], "Subgoal")
        self.assertEqual(audit["child_goal_requests"][0]["goal_text"], "Implement child task")

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

    def test_httpbridge_source_opens_session_map_when_no_session_is_requested(self) -> None:
        source = (SRC / "runtime" / "cli_service_adapter.py").read_text(encoding="utf-8")
        self.assertIn("initial_session_map_open = requested_session_id(self, query=query) is None", source)
        self.assertIn('f"let sessionMapOpen = {json.dumps(initial_session_map_open)};"', source)
        self.assertIn("setSessionMapOpen(sessionMapOpen);", source)

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
