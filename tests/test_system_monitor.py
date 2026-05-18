from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from kernel.lifecycle import register_process, update_process_fields, write_lifecycle_state
from kernel.registry import register_service, update_service_process, write_registry
from runtime.persistent_state_pkg import (
    append_history,
    append_pending_input,
    create_conversation_session,
    ensure_state,
    read_json_file,
    session_metadata_path,
    update_session_goal,
    write_json_file,
)
from runtime.system_monitor import scan_system_sessions
from wire.protocol import utc_ts


class SystemMonitorTests(unittest.TestCase):
    def test_scan_system_sessions_reports_pending_goal_stall_and_service_problem(self) -> None:
        with tempfile.TemporaryDirectory() as runtime_dir:
            runtime_root = Path(runtime_dir)
            ensure_state(runtime_root)

            waiting = create_conversation_session(runtime_root, username="root", label="Waiting")
            append_pending_input(
                runtime_root,
                username="root",
                session_id=str(waiting["session_id"]),
                entry={"kind": "user_message", "role": "user", "text": "still waiting"},
            )

            stalled = create_conversation_session(runtime_root, username="root", label="Stalled")
            update_session_goal(
                runtime_root,
                username="root",
                session_id=str(stalled["session_id"]),
                goal_text="Finish the stalled task",
            )
            stalled_metadata_path = session_metadata_path(
                runtime_root,
                username="root",
                session_id=str(stalled["session_id"]),
            )
            # Preserve the active goal state while making the session old enough to count as stalled.
            stalled_session = read_json_file(stalled_metadata_path)
            assert isinstance(stalled_session, dict)
            stalled_session["updated_at"] = "2026-05-18T08:00:00Z"
            write_json_file(stalled_metadata_path, stalled_session)

            active_turn = create_conversation_session(runtime_root, username="root", label="Active Turn")
            append_history(
                runtime_root,
                username="root",
                session_id=str(active_turn["session_id"]),
                entry={
                    "direction": "event",
                    "ts": "2026-05-18T08:30:00Z",
                    "service_id": "service-codex-001",
                    "event_type": "agent.turn_started",
                },
                limit=200,
            )

            broken = create_conversation_session(runtime_root, username="root", label="Broken Binding")
            broken_metadata_path = session_metadata_path(
                runtime_root,
                username="root",
                session_id=str(broken["session_id"]),
            )
            broken_session = read_json_file(broken_metadata_path)
            assert isinstance(broken_session, dict)
            broken_session["service_id"] = "service-codex-broken-001"
            broken_session["updated_at"] = utc_ts()
            write_json_file(broken_metadata_path, broken_session)

            write_registry(runtime_root, {"services": {}})
            write_lifecycle_state(runtime_root, {"processes": {}})

            register_service(
                runtime_root,
                service_spec={
                    "service_id": "service-codex-broken-001",
                    "kind": "codex",
                    "display_name": "Broken Codex",
                    "persona": "Broken",
                    "max_turns": 8,
                    "response_schema_id": "service_control_v1",
                },
            )
            register_process(
                runtime_root,
                process_id="proc-broken-001",
                service_id="service-codex-broken-001",
                node_id="node-1",
                status="running",
            )
            update_service_process(
                runtime_root,
                service_id="service-codex-broken-001",
                process_id="proc-broken-001",
                status="running",
            )
            update_process_fields(
                runtime_root,
                process_id="proc-broken-001",
                fields={"status": "dead"},
            )

            report = scan_system_sessions(
                runtime_root,
                now=datetime(2026, 5, 18, 10, 0, tzinfo=UTC),
                stalled_after_seconds=3600,
            )

            self.assertEqual(report["counts"]["sessions_scanned"], 4)
            self.assertEqual(report["counts"]["findings"], 4)
            self.assertEqual(report["counts"]["unresolved_user_input"], 1)
            self.assertEqual(report["counts"]["unfinished_goals"], 1)
            self.assertEqual(report["counts"]["stalled"], 2)
            self.assertEqual(report["counts"]["system_problems"], 1)

            findings = {item["label"]: item for item in report["findings"]}
            self.assertTrue(findings["Waiting"]["unresolved_user_input"])
            self.assertEqual(findings["Waiting"]["pending_user_input_count"], 1)
            self.assertTrue(findings["Stalled"]["unfinished_goal"])
            self.assertIn(
                "unfinished_goal_without_recent_session_update",
                findings["Stalled"]["stalled_reasons"],
            )
            self.assertIn(
                "agent_turn_exceeded_threshold",
                findings["Active Turn"]["stalled_reasons"],
            )
            self.assertEqual(
                findings["Broken Binding"]["system_problem"],
                "bound_service_process_status:dead",
            )

    def test_scan_system_sessions_ignores_stale_turn_for_completed_session(self) -> None:
        with tempfile.TemporaryDirectory() as runtime_dir:
            runtime_root = Path(runtime_dir)
            ensure_state(runtime_root)

            completed = create_conversation_session(runtime_root, username="root", label="Completed")
            completed_session_id = str(completed["session_id"])
            update_session_goal(
                runtime_root,
                username="root",
                session_id=completed_session_id,
                goal_text="Finish and archive the work",
            )
            completed_metadata_path = session_metadata_path(
                runtime_root,
                username="root",
                session_id=completed_session_id,
            )
            completed_session = read_json_file(completed_metadata_path)
            assert isinstance(completed_session, dict)
            completed_session["goal_completed"] = True
            completed_session["goal_progress_state"] = "complete"
            completed_session["updated_at"] = "2026-05-18T09:00:00Z"
            goal_history = completed_session.get("goal_history")
            if isinstance(goal_history, list) and goal_history:
                goal_history[-1]["goal_completed"] = True
                goal_history[-1]["goal_progress_state"] = "complete"
                goal_history[-1]["updated_at"] = "2026-05-18T09:00:00Z"
            write_json_file(completed_metadata_path, completed_session)
            append_history(
                runtime_root,
                username="root",
                session_id=completed_session_id,
                entry={
                    "direction": "event",
                    "ts": "2026-05-18T08:30:00Z",
                    "service_id": "service-codex-001",
                    "event_type": "agent.turn_started",
                },
                limit=200,
            )

            report = scan_system_sessions(
                runtime_root,
                now=datetime(2026, 5, 18, 10, 0, tzinfo=UTC),
                stalled_after_seconds=3600,
            )

            self.assertEqual(report["counts"]["sessions_scanned"], 1)
            self.assertEqual(report["counts"]["findings"], 0)
            self.assertEqual(report["counts"]["stalled"], 0)


if __name__ == "__main__":
    unittest.main()
