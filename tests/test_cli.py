from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import Future
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
ENV = {"PYTHONPATH": str(ROOT / "src"), "AIZE_ENABLE_EXTERNAL_AGENTS": "false"}


def run_cli(state_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "cli", "--root", str(state_root), *args],
        cwd=ROOT,
        env=ENV,
        text=True,
        capture_output=True,
        check=False,
    )


def run_cli_with_input(state_root: Path, input_text: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "cli", "--root", str(state_root), *args],
        cwd=ROOT,
        env=ENV,
        text=True,
        input=input_text,
        capture_output=True,
        check=False,
    )


def run_cli_with_env(state_root: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "cli", "--root", str(state_root), *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def message_body(message: dict) -> str:
    payload = message.get("payload")
    if isinstance(payload, dict):
        return str(payload.get("body") or payload.get("text") or "")
    return ""


def wait_for_goal_state(state_root: Path, session_id: str, expected_state: str, *, timeout: float = 5.0) -> dict[str, str]:
    deadline = time.monotonic() + timeout
    last_goal: dict[str, str] | None = None
    while time.monotonic() < deadline:
        result = run_cli(state_root, "goals", session_id)
        if result.returncode == 0:
            goals = json.loads(result.stdout)
            if goals:
                last_goal = goals[0]
                if goals[0].get("completion_state") == expected_state:
                    return goals[0]
        time.sleep(0.05)
    raise AssertionError(f"goal did not become {expected_state}: {last_goal}")


class CliTests(unittest.TestCase):
    def test_agent_allocation_counts_follow_current_dispatch_phase(self) -> None:
        from cli_render import agent_allocations_by_session, agent_pool_snapshot
        from store_defs import GOAL_MANAGER_ROLE, WORKER_AGENT_ROLE

        runs = [
            {
                "run_id": "run-gm",
                "session_id": "session-gm",
                "goal_id": "goal-gm",
                "lease_state": "acquired",
                "current_phase": "GoalManager",
            },
            {
                "run_id": "run-worker",
                "session_id": "session-worker",
                "goal_id": "goal-worker",
                "lease_state": "acquired",
                "current_phase": "WorkerAgent",
            },
        ]

        allocations = agent_allocations_by_session(runs)
        self.assertEqual(allocations["session-gm"], {GOAL_MANAGER_ROLE: 1, WORKER_AGENT_ROLE: 0})
        self.assertEqual(allocations["session-worker"], {GOAL_MANAGER_ROLE: 0, WORKER_AGENT_ROLE: 1})

        pool = agent_pool_snapshot(
            [
                {"role": GOAL_MANAGER_ROLE, "provider": "codex", "status": "active"},
                {"role": WORKER_AGENT_ROLE, "provider": "codex", "status": "active"},
            ],
            runs,
        )
        self.assertEqual([role["allocated"] for role in pool["roles"]], [1, 1])
        self.assertEqual([run["current_phase"] for run in pool["active_runs"]], ["GoalManager", "WorkerAgent"])

    def test_goal_manager_status_and_reason_can_be_embedded_in_xml_body(self) -> None:
        from store import Store

        store = Store(Path("/unused"))
        output = (
            "<aize-output>\n"
            "  <body>AIZE_GOAL_STATUS: completed\n"
            "AIZE_GOAL_REASON: answered the user</body>\n"
            "</aize-output>"
        )
        self.assertEqual(store._extract_goal_manager_status(output), "completed")
        self.assertEqual(store._extract_goal_manager_reason(output), "answered the user")
        tagged_output = (
            "<aize-output>\n"
            "  <AIZE_GOAL_STATUS>completed</AIZE_GOAL_STATUS>\n"
            "  <AIZE_GOAL_REASON>answered through explicit XML tags</AIZE_GOAL_REASON>\n"
            "</aize-output>"
        )
        self.assertEqual(store._extract_goal_manager_status(tagged_output), "completed")
        self.assertEqual(store._extract_goal_manager_reason(tagged_output), "answered through explicit XML tags")

    def test_console_body_prefers_agent_output_before_embedded_prompt(self) -> None:
        from cli_render import console_body

        output = (
            "<aize-output>\n"
            "  <body>It is 2026-06-20 20:26:17 JST.</body>\n"
            "</aize-output>\n\n"
            "stderr:\n"
            "<aize-agent-input><body>what time is it now?</body></aize-agent-input>"
        )
        self.assertEqual(console_body(output), "It is 2026-06-20 20:26:17 JST.")

        goal_manager_output = (
            "<aize-output>\n"
            "  <message>AIZE_GOAL_STATUS: completed\n"
            "AIZE_GOAL_REASON: answered with the current time</message>\n"
            "</aize-output>"
        )
        self.assertEqual(console_body(goal_manager_output), "answered with the current time")

    def test_minimal_message_passing_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"

            initialized = run_cli(state_root, "init")
            self.assertEqual(initialized.returncode, 0)
            initial_state = json.loads(initialized.stdout)
            self.assertEqual(initial_state["units"]["root"]["instance_policy"], "singleton")
            self.assertEqual(initial_state["units"]["root"]["singleton_session_id"], "root")
            self.assertTrue(initial_state["sessions"]["root"]["singleton"])
            self.assertIn("root", initial_state["accounts"])
            self.assertNotEqual(initial_state["accounts"]["root"]["password_hash"], "root")
            self.assertEqual(initial_state["agent_profiles"]["GoalManager"]["provider"], "codex")
            self.assertEqual(initial_state["agent_profiles"]["WorkerAgent"]["provider"], "codex")

            self.assertEqual(run_cli(state_root, "create-unit", "entrance").returncode, 0)
            self.assertEqual(run_cli(state_root, "create-session", "entrance-main", "--unit", "entrance").returncode, 0)

            sent = run_cli(state_root, "send", "root", "user", "kernel", "hello")
            self.assertEqual(sent.returncode, 0, sent.stderr)
            message = json.loads(sent.stdout)
            self.assertEqual(message["from"], "account:user")
            self.assertEqual(message["to"], "account:kernel")
            self.assertEqual(message["payload"]["body"], "hello")

            payload_file = state_root.parent / "payload.txt"
            payload_file.write_text("payload body\n", encoding="utf-8")
            sent_file = run_cli(
                state_root,
                "send-file",
                "root",
                "user",
                "kernel",
                str(payload_file),
                "--body",
                "attached payload",
            )
            self.assertEqual(sent_file.returncode, 0, sent_file.stderr)
            file_message = json.loads(sent_file.stdout)
            self.assertEqual(file_message["payload"]["body"], "attached payload")
            self.assertEqual(file_message["payload"]["files"][0]["file_name"], "payload.txt")
            self.assertEqual(file_message["payload"]["files"][0]["content"], "payload body\n")

            status = json.loads(run_cli(state_root, "status").stdout)
            self.assertEqual(status["unit_count"], 2)
            self.assertEqual(status["session_count"], 2)
            self.assertEqual(status["active_session_count"], 2)
            self.assertEqual(status["inactive_session_count"], 0)
            self.assertEqual(status["session_edge_count"], 1)
            self.assertEqual(status["account_count"], 1)
            self.assertEqual(status["message_count"], 2)
            self.assertEqual(status["endpoint_cursor_count"], 0)

            received = run_cli(state_root, "recv", "kernel")
            self.assertEqual(received.returncode, 0, received.stderr)
            delivered = json.loads(received.stdout)
            self.assertEqual(delivered["to"], "account:kernel")

            status = json.loads(run_cli(state_root, "status").stdout)
            self.assertEqual(status["message_count"], 2)
            self.assertEqual(status["endpoint_cursor_count"], 1)

            sessions = json.loads(run_cli(state_root, "sessions").stdout)
            self.assertEqual(sessions[0]["session_id"], "entrance-main")
            self.assertEqual(sessions[1]["session_id"], "root")

            units = json.loads(run_cli(state_root, "units").stdout)
            self.assertEqual(units[0]["unit_id"], "entrance")
            self.assertEqual(units[1]["unit_id"], "root")

            children = json.loads(run_cli(state_root, "children", "root").stdout)
            self.assertEqual(children[0]["child_session_id"], "entrance-main")

            accounts = json.loads(run_cli(state_root, "accounts").stdout)
            self.assertEqual(accounts[0]["username"], "root")
            self.assertEqual(accounts[0]["roles"], ["root", "admin"])
            self.assertNotIn("password_hash", accounts[0])
            self.assertNotIn("salt", accounts[0])

            auth = run_cli(state_root, "auth", "root", "root")
            self.assertEqual(auth.returncode, 0, auth.stderr)
            self.assertEqual(json.loads(auth.stdout)["username"], "root")

            failed_auth = run_cli(state_root, "auth", "root", "wrong")
            self.assertEqual(failed_auth.returncode, 2)
            self.assertIn("authentication failed", failed_auth.stderr)

    def test_unit_goal_prompt_and_interval_schedule_start_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"

            self.assertEqual(run_cli(state_root, "init").returncode, 0)
            created = run_cli(
                state_root,
                "create-unit",
                "monitor",
                "--display-name",
                "Monitor",
                "--description",
                "scheduled monitor",
                "--goal-text",
                "Inspect system state and report findings.",
                "--initial-prompt",
                "Run diagnostics now.",
                "--schedule-every-hours",
                "1",
                "--schedule-next-run-at",
                "2026-06-22T00:00:00Z",
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            unit = json.loads(created.stdout)
            self.assertEqual(unit["goal_text"], "Inspect system state and report findings.")
            self.assertEqual(unit["initial_prompt"], "Run diagnostics now.")
            self.assertEqual(unit["schedule"]["every_hours"], 1)
            self.assertEqual(unit["schedule"]["next_run_at"], "2026-06-22T00:00:00Z")

            started = run_cli(
                state_root,
                "run-scheduled-units",
                "--now",
                "2026-06-22T00:00:00Z",
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            payload = json.loads(started.stdout)
            self.assertEqual(len(payload), 1)
            self.assertEqual(payload[0]["session"]["unit_id"], "monitor")
            self.assertEqual(payload[0]["goal"]["body"], "Inspect system state and report findings.")
            self.assertEqual(payload[0]["initial_message"]["payload"]["body"], "Run diagnostics now.")
            self.assertTrue(payload[0]["initial_message"]["payload"]["user_input"])

            messages = json.loads(
                run_cli(
                    state_root,
                    "messages",
                    payload[0]["session"]["session_id"],
                    "--limit",
                    "0",
                ).stdout
            )
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0]["payload"]["scheduled_unit_id"], "monitor")
            units_after_first_run = json.loads(run_cli(state_root, "units").stdout)
            monitor = next(unit for unit in units_after_first_run if unit["unit_id"] == "monitor")
            self.assertEqual(monitor["schedule"]["last_run_at"], "2026-06-22T00:00:00Z")
            self.assertEqual(monitor["schedule"]["next_run_at"], "2026-06-22T01:00:00Z")

            not_due = run_cli(
                state_root,
                "run-scheduled-units",
                "--now",
                "2026-06-22T00:30:00Z",
            )
            self.assertEqual(json.loads(not_due.stdout), [])

            due_again = run_cli(
                state_root,
                "run-scheduled-units",
                "--now",
                "2026-06-22T01:00:00Z",
            )
            self.assertEqual(len(json.loads(due_again.stdout)), 1)
            units_after_second_run = json.loads(run_cli(state_root, "units").stdout)
            monitor = next(unit for unit in units_after_second_run if unit["unit_id"] == "monitor")
            self.assertEqual(monitor["schedule"]["next_run_at"], "2026-06-22T02:00:00Z")

    def test_late_scheduled_unit_run_advances_next_run_to_future_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"

            self.assertEqual(run_cli(state_root, "init").returncode, 0)
            created = run_cli(
                state_root,
                "create-unit",
                "monitor",
                "--goal-text",
                "Inspect system state and report findings.",
                "--schedule-every-hours",
                "1",
                "--schedule-next-run-at",
                "2026-06-22T00:00:00Z",
            )
            self.assertEqual(created.returncode, 0, created.stderr)

            started = run_cli(
                state_root,
                "run-scheduled-units",
                "--now",
                "2026-06-22T02:30:00Z",
            )
            self.assertEqual(len(json.loads(started.stdout)), 1)
            units = json.loads(run_cli(state_root, "units").stdout)
            monitor = next(unit for unit in units if unit["unit_id"] == "monitor")
            self.assertEqual(monitor["schedule"]["next_run_at"], "2026-06-22T03:00:00Z")

    def test_daemon_starts_due_scheduled_units_and_dispatches_them(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"

            self.assertEqual(run_cli(state_root, "init").returncode, 0)
            created = run_cli(
                state_root,
                "create-unit",
                "monitor",
                "--goal-text",
                "Inspect system state and report findings.",
                "--initial-prompt",
                "Run diagnostics now.",
                "--schedule-every-hours",
                "1",
            )
            self.assertEqual(created.returncode, 0, created.stderr)

            daemon = run_cli(
                state_root,
                "daemon",
                "--max-cycles",
                "3",
                "--schedule-interval",
                "60",
                "--dispatch-interval",
                "0.01",
            )
            self.assertEqual(daemon.returncode, 0, daemon.stderr)
            payload = json.loads(daemon.stdout)
            self.assertEqual(payload["scheduled_count"], 1)
            self.assertGreaterEqual(payload["dispatched_count"], 1)
            session_id = payload["scheduled"][0]["session"]["session_id"]

            sessions = json.loads(run_cli(state_root, "sessions").stdout)
            self.assertTrue(any(session["session_id"] == session_id for session in sessions))
            runs = json.loads(run_cli(state_root, "dispatch-runs", session_id).stdout)
            self.assertTrue(runs)

    def test_daemon_dispatch_lots_run_interchangeable_parallel_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"

            self.assertEqual(run_cli(state_root, "init").returncode, 0)
            for session_id in ("alpha", "beta"):
                self.assertEqual(run_cli(state_root, "create-session", session_id).returncode, 0)
                self.assertEqual(
                    run_cli(
                        state_root,
                        "update-goal",
                        session_id,
                        f"reply for {session_id}",
                        "--created-by",
                        "root",
                    ).returncode,
                    0,
                )

            daemon = run_cli(
                state_root,
                "daemon",
                "--dispatch-lots",
                "2",
                "--max-cycles",
                "1",
                "--schedule-interval",
                "60",
                "--dispatch-interval",
                "0.01",
            )
            self.assertEqual(daemon.returncode, 0, daemon.stderr)
            payload = json.loads(daemon.stdout)
            self.assertEqual(payload["dispatch_lot_size"], 2)
            self.assertEqual(payload["dispatch_lot_cap"], 10)
            self.assertEqual(payload["dispatched_count"], 2)
            self.assertEqual(payload["peak_active_dispatch_lots"], 2)

            runs = json.loads(run_cli(state_root, "dispatch-runs").stdout)
            lot_ids = sorted(run.get("dispatch_lot_id") for run in runs)
            self.assertEqual(lot_ids, [1, 2])
            self.assertEqual({run["session_id"] for run in runs}, {"alpha", "beta"})

    def test_dispatch_lot_size_can_change_after_daemon_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"

            self.assertEqual(run_cli(state_root, "init").returncode, 0)
            first = run_cli(state_root, "set-dispatch-lots", "3")
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(json.loads(first.stdout)["dispatch_lot_size"], 3)
            status = json.loads(run_cli(state_root, "status").stdout)
            self.assertEqual(status["dispatch_lot_size"], 3)

            second = run_cli(state_root, "set-dispatch-lots", "1")
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(json.loads(second.stdout)["dispatch_lot_size"], 1)
            status = json.loads(run_cli(state_root, "status").stdout)
            self.assertEqual(status["dispatch_lot_size"], 1)

            rejected = run_cli(state_root, "set-dispatch-lots", "0")
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("dispatch lot size must be positive", rejected.stderr)

    def test_dispatch_lot_refill_respects_dynamic_target_without_pinning_sessions(self) -> None:
        from cli_workers import _collect_completed_lots, _submit_available_lots

        class FakeExecutor:
            def __init__(self) -> None:
                self.submitted: list[int] = []

            def submit(self, func, **kwargs):
                lot_id = int(kwargs["dispatch_lot_id"])
                self.submitted.append(lot_id)
                future: Future = Future()
                future.set_result(None)
                return future

        class FakeStore:
            def dispatch_once(self, **kwargs):
                return None

        active: dict[int, Future] = {}
        for lot_id in (1, 2, 3):
            future: Future = Future()
            active[lot_id] = future

        executor = FakeExecutor()
        submitted = _submit_available_lots(
            executor,
            active,
            target_lots=1,
            recovery_context=None,
            store=FakeStore(),
        )
        self.assertEqual(submitted, [])
        self.assertEqual(set(active), {1, 2, 3})

        active[1].set_result(None)
        active[2].set_result(None)
        dispatched: list[dict] = []
        completed = _collect_completed_lots(active, dispatched)
        self.assertEqual(completed, [1, 2])
        self.assertEqual(set(active), {3})

        active[3].set_result(None)
        _collect_completed_lots(active, dispatched)
        submitted = _submit_available_lots(
            executor,
            active,
            target_lots=2,
            recovery_context=None,
            store=FakeStore(),
        )
        self.assertEqual(submitted, [1, 2])
        self.assertEqual(executor.submitted, [1, 2])

    def test_session_graph_rejects_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"

            self.assertEqual(run_cli(state_root, "init").returncode, 0)
            self.assertEqual(run_cli(state_root, "create-unit", "worker").returncode, 0)
            self.assertEqual(run_cli(state_root, "create-session", "dev", "--unit", "worker").returncode, 0)
            self.assertEqual(
                run_cli(
                    state_root,
                    "create-session",
                    "task",
                    "--unit",
                    "worker",
                    "--parent",
                    "root",
                    "--parent",
                    "dev",
                ).returncode,
                0,
            )

            graph = json.loads(run_cli(state_root, "session-graph").stdout)
            self.assertEqual(len(graph["edges"]), 3)

            parents = json.loads(run_cli(state_root, "parents", "task").stdout)
            self.assertEqual([parent["parent_session_id"] for parent in parents], ["dev", "root"])

            children = json.loads(run_cli(state_root, "children", "root").stdout)
            self.assertEqual([child["child_session_id"] for child in children], ["dev", "task"])

            cycle = run_cli(state_root, "link-session", "task", "root")
            self.assertEqual(cycle.returncode, 2)
            self.assertIn("cycle", cycle.stderr)

    def test_create_session_requires_existing_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"

            self.assertEqual(run_cli(state_root, "init").returncode, 0)
            self.assertEqual(run_cli(state_root, "create-unit", "worker").returncode, 0)
            units = json.loads(run_cli(state_root, "units").stdout)
            worker_unit = next(unit for unit in units if unit["unit_id"] == "worker")
            self.assertTrue(worker_unit["workspace_path"].startswith("workspaces/units/worker-"))
            worker_workspace = state_root / worker_unit["workspace_path"]
            self.assertTrue(worker_workspace.is_dir())

            plain = run_cli(state_root, "create-session", "plain")
            self.assertEqual(plain.returncode, 0, plain.stderr)
            plain_session = json.loads(plain.stdout)
            self.assertEqual(plain_session["session_id"], "plain")
            self.assertIsNone(plain_session["unit_id"])
            self.assertTrue(plain_session["workspace_path"].startswith("workspaces/sessions/plain-"))
            self.assertTrue((state_root / plain_session["workspace_path"]).is_dir())

            sessions = json.loads(run_cli(state_root, "sessions").stdout)
            root_session = next(session for session in sessions if session["session_id"] == "root")
            self.assertTrue(root_session["workspace_path"].startswith("workspaces/sessions/root-"))
            self.assertTrue((state_root / root_session["workspace_path"]).is_dir())

            unit_backed = run_cli(state_root, "create-session", "unit-backed", "--unit", "worker")
            self.assertEqual(unit_backed.returncode, 0, unit_backed.stderr)
            unit_session = json.loads(unit_backed.stdout)
            unit_link = state_root / unit_session["workspace_path"] / "unit-workspace"
            self.assertTrue(unit_link.is_symlink())
            self.assertEqual(unit_link.resolve(), worker_workspace.resolve())

            orphan = run_cli(state_root, "create-session", "orphan", "--unit", "worker", "--parent", "missing")
            self.assertEqual(orphan.returncode, 2)
            self.assertIn("unknown parent session", orphan.stderr)

    def test_start_goal_creates_child_session_and_goal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"

            self.assertEqual(run_cli(state_root, "init").returncode, 0)
            self.assertEqual(run_cli(state_root, "create-unit", "worker").returncode, 0)

            started = run_cli(
                state_root,
                "start-goal",
                "build-cli",
                "--unit",
                "worker",
                "--parent",
                "root",
                "--label",
                "Build CLI",
                "--body",
                "interactive session",
                "--created-by",
                "root",
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            payload = json.loads(started.stdout)
            self.assertEqual(payload["session"]["session_id"], "build-cli")
            self.assertTrue(payload["session"]["active"])
            self.assertEqual(payload["goal"]["session_id"], "build-cli")
            self.assertEqual(payload["goal"]["completion_state"], "incomplete")

            goals = json.loads(run_cli(state_root, "goals", "build-cli").stdout)
            self.assertEqual(payload["session"]["title"], "Build CLI")
            self.assertEqual(goals[0]["body"], "interactive session")
            self.assertEqual(goals[0]["completion_state"], "incomplete")

            user_input = run_cli(state_root, "user-input", "build-cli", "root", "please build via session message")
            self.assertEqual(user_input.returncode, 0, user_input.stderr)
            user_message = json.loads(user_input.stdout)
            self.assertEqual(user_message["from"], "account:root")
            self.assertEqual(user_message["to"], "session:build-cli")
            self.assertTrue(user_message["payload"]["user_input"])

            children = json.loads(run_cli(state_root, "children", "root").stdout)
            self.assertEqual(children[0]["child_session_id"], "build-cli")

            dispatched = run_cli(state_root, "dispatch-once")
            self.assertEqual(dispatched.returncode, 0, dispatched.stderr)
            dispatch_payload = json.loads(dispatched.stdout)
            self.assertEqual(dispatch_payload["goal"]["completion_state"], "complete")
            self.assertEqual(dispatch_payload["unit"]["unit_id"], "worker")
            self.assertIsNone(dispatch_payload["message"])
            self.assertEqual(dispatch_payload["state_transition"]["completion_state"], "complete")
            self.assertEqual(dispatch_payload["run"]["completion_state"], "complete")
            self.assertEqual(dispatch_payload["run"]["lease_state"], "released")
            self.assertEqual(
                [step["phase"] for step in dispatch_payload["run"]["steps"]],
                ["GoalManagerReview"],
            )
            self.assertEqual(len(dispatch_payload["run"]["session_message_ids"]), 1)

            goals = json.loads(run_cli(state_root, "goals", "build-cli").stdout)
            self.assertEqual(goals[0]["completion_state"], "complete")

            messages = json.loads(run_cli(state_root, "messages", "build-cli").stdout)
            self.assertEqual(len(messages), 1)
            self.assertTrue(messages[0]["payload"]["user_input"])
            self.assertFalse(any("goal_completion_state" in message.get("payload", {}) for message in messages))
            self.assertFalse(any("session_capabilities" in message.get("payload", {}) for message in messages))
            self.assertFalse(any(message.get("to") == "agent:GoalManager" for message in messages))

            dispatch_runs = json.loads(run_cli(state_root, "dispatch-runs", "build-cli").stdout)
            self.assertEqual(dispatch_runs[0]["completion_state"], "complete")
            self.assertEqual(dispatch_runs[0]["lease_state"], "released")

            agent_threads = json.loads(run_cli(state_root, "agent-threads", "build-cli").stdout)
            self.assertEqual([thread["role"] for thread in agent_threads], ["GoalManager"])
            self.assertEqual([len(thread["turns"]) for thread in agent_threads], [1])
            self.assertIn("<aize-message-bundle>", agent_threads[0]["turns"][0]["prompt"])
            self.assertIn("please build via session message", agent_threads[0]["turns"][0]["prompt"])

            followup = run_cli(state_root, "user-input", "build-cli", "root", "follow up after completion")
            self.assertEqual(followup.returncode, 0, followup.stderr)
            goals = json.loads(run_cli(state_root, "goals", "build-cli").stdout)
            self.assertEqual(goals[0]["completion_state"], "incomplete")
            queue = json.loads(run_cli(state_root, "dispatch-index", "build-cli").stdout)
            self.assertEqual(queue[0]["status"], "queued")
            self.assertEqual(queue[0]["priority"], 100)

            redispatched = run_cli(state_root, "dispatch-once")
            self.assertEqual(redispatched.returncode, 0, redispatched.stderr)
            self.assertEqual(json.loads(redispatched.stdout)["goal"]["completion_state"], "complete")

            empty_dispatch = json.loads(run_cli(state_root, "dispatch-once").stdout)
            self.assertIsNone(empty_dispatch["dispatched"])

    def test_dispatch_recovery_context_is_run_metadata_not_session_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            recovery_context = "CLI restarted; continue the current SessionGoal from persisted state."

            self.assertEqual(run_cli(state_root, "init").returncode, 0)
            self.assertEqual(run_cli(state_root, "create-session", "resume-session").returncode, 0)
            self.assertEqual(
                run_cli(
                    state_root,
                    "update-goal",
                    "resume-session",
                    "Resume work",
                    "--created-by",
                    "root",
                ).returncode,
                0,
            )

            dispatched = run_cli(state_root, "dispatch-once", "--recovery-context", recovery_context)
            self.assertEqual(dispatched.returncode, 0, dispatched.stderr)
            payload = json.loads(dispatched.stdout)
            self.assertEqual(payload["run"]["recovery_context"], recovery_context)

            messages = json.loads(run_cli(state_root, "messages", "resume-session", "-n", "0").stdout)
            self.assertFalse(any(recovery_context in json.dumps(message) for message in messages))

            threads = json.loads(run_cli(state_root, "agent-threads", "resume-session").stdout)
            prompts = "\n".join(turn["prompt"] for thread in threads for turn in thread["turns"])
            self.assertIn("<recovery-context>", prompts)
            self.assertIn(recovery_context, prompts)

    def test_agent_api_sends_to_reply_console_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from agent_api import (
                send_message,
                send_session_message,
                send_user_console_message,
                send_worker_request,
            )
            from store import Store
            from store_defs import StoreError

            state_root = Path(tmp) / "state"
            store = Store(state_root)
            store.init()
            store.create_session("api-session", parent_session_ids=["root"])
            user_message = store.append_user_input(
                "api-session",
                sender="root",
                body="hello",
                reply_to="console-test",
            )
            self.assertEqual(user_message["payload"]["reply_to"], "console-test")

            previous_env = {
                key: os.environ.get(key)
                for key in ("AIZE_STATE_ROOT", "AIZE_SESSION_ID", "AIZE_AGENT_ROLE", "AIZE_RUN_ID")
            }
            try:
                os.environ["AIZE_STATE_ROOT"] = str(state_root)
                os.environ["AIZE_SESSION_ID"] = "api-session"
                os.environ["AIZE_RUN_ID"] = "run-test"

                os.environ["AIZE_AGENT_ROLE"] = "GoalManager"
                console_message = send_user_console_message("reply through API")
                self.assertEqual(console_message["to"], "console:console-test")
                self.assertEqual(console_message["payload"]["run_id"], "run-test")
                worker_request = send_worker_request("please do the work")
                self.assertEqual(worker_request["to"], "session:api-session")
                self.assertTrue(worker_request["payload"]["worker_request"])
                self.assertEqual(worker_request["payload"]["worker_role"], "WorkerAgent")

                os.environ["AIZE_AGENT_ROLE"] = "WorkerAgent"
                session_message = send_session_message("session note through API")
                self.assertEqual(session_message["to"], "session:api-session")

                with self.assertRaises(StoreError):
                    send_user_console_message("worker must not reply to console")

                with self.assertRaises(StoreError):
                    send_worker_request("worker must not delegate worker")

                with self.assertRaises(StoreError):
                    send_message("GoalManager", "worker must report through Session instead")

                with self.assertRaises(StoreError):
                    send_message("root", "invalid route")
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

    def test_user_input_fans_out_to_active_worker_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from store import Store
            from model import utc_now

            state_root = Path(tmp) / "state"
            store = Store(state_root)
            store.init()
            store.create_session("running-session", parent_session_ids=["root"])
            state = store.load()
            now = utc_now()
            state["dispatch_runs"]["run-active-worker"] = {
                "run_id": "run-active-worker",
                "goal_id": "goal-placeholder",
                "session_id": "running-session",
                "lease_state": "acquired",
                "current_phase": "WorkerAgent",
                "created_at": now,
                "lease_acquired_at": now,
                "steps": [],
            }
            store.save(state)

            message = store.append_user_input(
                "running-session",
                sender="root",
                body="new user input while worker is running",
                reply_to="console-running",
            )
            self.assertEqual(message["to"], "session:running-session")

            messages = store.messages("running-session")
            worker_messages = [
                item
                for item in messages
                if item.get("to") == "session:running-session"
                and item.get("payload", {}).get("worker_request") is True
            ]
            self.assertEqual(len(worker_messages), 1)
            self.assertEqual(worker_messages[0]["payload"]["forwarded_from"], message["message_id"])
            self.assertEqual(worker_messages[0]["payload"]["run_id"], "run-active-worker")
            self.assertEqual(worker_messages[0]["payload"]["body"], "new user input while worker is running")
            queue = store.dispatch_queue("running-session")
            worker_entries = [
                entry
                for entry in queue
                if entry.get("status") == "queued"
                and entry.get("role") == "WorkerAgent"
                and entry.get("trigger_message_id") == worker_messages[0]["message_id"]
            ]
            self.assertEqual(len(worker_entries), 1)

    def test_active_worker_followup_waits_for_worker_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from store import Store
            from model import Goal, utc_now

            state_root = Path(tmp) / "state"
            store = Store(state_root)
            store.init()
            store.create_session("running-session", parent_session_ids=["root"])
            now = utc_now()
            goal = Goal(
                goal_id="goal-active-worker",
                session_id="running-session",
                body="handle active worker followup",
                created_by="root",
                created_at=now,
            ).to_dict()
            state = store.load()
            state["agent_profiles"]["WorkerAgent"]["provider"] = "local"
            state["goals"][goal["goal_id"]] = goal
            state["dispatch_runs"]["run-active-worker"] = {
                "run_id": "run-active-worker",
                "goal_id": goal["goal_id"],
                "session_id": "running-session",
                "role": "WorkerAgent",
                "lease_state": "acquired",
                "current_phase": "WorkerAgent",
                "created_at": now,
                "lease_acquired_at": now,
                "steps": [],
            }
            store.save(state)

            message = store.append_user_input(
                "running-session",
                sender="root",
                body="new user input while worker is running",
            )
            state = store.load()
            worker_message = next(
                item
                for item in state["messages"]
                if item.get("payload", {}).get("forwarded_from") == message["message_id"]
            )
            self.assertIsNone(store.dispatch_once(session_id="running-session"))

            state = store.load()
            state["dispatch_runs"]["run-active-worker"]["lease_state"] = "released"
            state["dispatch_runs"]["run-active-worker"].pop("current_phase", None)
            store.save(state)

            dispatched = store.dispatch_once(session_id="running-session")
            self.assertIsNotNone(dispatched)
            assert dispatched is not None
            self.assertEqual(dispatched["run"]["role"], "WorkerAgent")
            self.assertEqual(dispatched["run"]["trigger_message_id"], worker_message["message_id"])

    def test_worker_session_report_enqueues_goal_manager_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from store import Store

            state_root = Path(tmp) / "state"
            store = Store(state_root)
            store.init()
            store.create_session("worker-report-session", parent_session_ids=["root"])
            goal = store.update_goal(
                "worker-report-session",
                body="finish delegated work",
                created_by="root",
            )
            state = store.load()
            for entry in state["dispatch_queue"]:
                if entry.get("goal_id") == goal["goal_id"]:
                    entry["status"] = "resolved"
            store.save(state)

            message = store.append_runtime_message(
                "worker-report-session",
                sender="WorkerAgent",
                recipient="Session",
                body="WorkerAgent finished the implementation and recorded the result.",
                run_id="run-worker-report",
            )
            self.assertEqual(message["to"], "session:worker-report-session")

            queue = store.dispatch_queue("worker-report-session")
            live_entries = [
                entry
                for entry in queue
                if entry.get("status") == "queued"
                and entry.get("role") == "GoalManager"
                and entry.get("trigger_message_id") == message["message_id"]
            ]
            self.assertEqual(len(live_entries), 1)
            self.assertIn("WorkerAgent Session report", live_entries[0]["reason"])
            self.assertEqual(live_entries[0]["goal_id"], goal["goal_id"])

    def test_triggered_dispatch_entries_are_not_coalesced_across_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from store import Store

            state_root = Path(tmp) / "state"
            store = Store(state_root)
            store.init()
            store.create_session("trigger-session", parent_session_ids=["root"])
            goal = store.update_goal(
                "trigger-session",
                body="handle worker requests in order",
                created_by="root",
            )
            state = store.load()
            for entry in state["dispatch_queue"]:
                if entry.get("goal_id") == goal["goal_id"]:
                    entry["status"] = "resolved"
            store.save(state)

            first = store.append_runtime_message(
                "trigger-session",
                sender="GoalManager",
                recipient="Session",
                body="first worker request",
                run_id="run-gm",
                worker_request=True,
            )
            second = store.append_runtime_message(
                "trigger-session",
                sender="GoalManager",
                recipient="Session",
                body="second worker request",
                run_id="run-gm",
                worker_request=True,
            )

            queue = store.dispatch_queue("trigger-session")
            worker_entries = [
                entry
                for entry in queue
                if entry.get("status") == "queued" and entry.get("role") == "WorkerAgent"
            ]
            self.assertEqual([entry["trigger_message_id"] for entry in worker_entries], [first["message_id"], second["message_id"]])

    def test_messages_defaults_to_tail_ten_and_accepts_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"

            self.assertEqual(run_cli(state_root, "init").returncode, 0)
            self.assertEqual(run_cli(state_root, "create-session", "tail-session", "--parent", "root").returncode, 0)
            for index in range(12):
                result = run_cli(
                    state_root,
                    "send",
                    "tail-session",
                    "tester",
                    "Session",
                    f"message-{index}",
                )
                self.assertEqual(result.returncode, 0, result.stderr)

            default_messages = json.loads(run_cli(state_root, "messages", "tail-session").stdout)
            self.assertEqual(len(default_messages), 10)
            self.assertEqual(message_body(default_messages[0]), "message-2")
            self.assertEqual(message_body(default_messages[-1]), "message-11")

            limited_messages = json.loads(run_cli(state_root, "messages", "tail-session", "--limit", "3").stdout)
            self.assertEqual([message_body(message) for message in limited_messages], ["message-9", "message-10", "message-11"])

            all_messages = json.loads(run_cli(state_root, "messages", "tail-session", "--limit", "0").stdout)
            self.assertEqual(len(all_messages), 12)
            self.assertEqual(message_body(all_messages[0]), "message-0")

    def test_batch_dispatch_resumes_same_session_threads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"

            self.assertEqual(run_cli(state_root, "init").returncode, 0)
            self.assertEqual(run_cli(state_root, "create-unit", "worker").returncode, 0)
            self.assertEqual(
                run_cli(
                    state_root,
                    "start-goal",
                    "batch-session",
                    "--unit",
                    "worker",
                    "--parent",
                    "root",
                    "--label",
                    "First",
                    "--body",
                    "first body",
                    "--created-by",
                    "root",
                ).returncode,
                0,
            )
            added = run_cli(
                state_root,
                "update-goal",
                "batch-session",
                "second body",
                "--created-by",
                "root",
            )
            self.assertEqual(added.returncode, 0, added.stderr)

            dispatched = run_cli(state_root, "dispatch-loop", "--limit", "10")
            self.assertEqual(dispatched.returncode, 0, dispatched.stderr)
            payload = json.loads(dispatched.stdout)
            self.assertEqual(payload["dispatched_count"], 1)

            goals = json.loads(run_cli(state_root, "goals", "batch-session").stdout)
            self.assertEqual(len(goals), 1)
            self.assertEqual(goals[0]["body"], "second body")
            self.assertEqual(goals[0]["completion_state"], "complete")

            threads = json.loads(run_cli(state_root, "agent-threads", "batch-session").stdout)
            self.assertEqual([thread["role"] for thread in threads], ["GoalManager"])
            self.assertEqual([len(thread["turns"]) for thread in threads], [1])
            self.assertTrue(threads[0]["resume_token"].startswith("thread-agent-"))

            runs = json.loads(run_cli(state_root, "dispatch-runs", "batch-session").stdout)
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["completion_state"], "complete")
            self.assertEqual(runs[0]["lease_state"], "released")

    def test_dispatch_worker_waits_for_new_user_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"

            self.assertEqual(run_cli(state_root, "init").returncode, 0)
            self.assertEqual(run_cli(state_root, "create-session", "time").returncode, 0)
            self.assertEqual(
                run_cli(
                    state_root,
                    "update-goal",
                    "time",
                    "Reply",
                    "--created-by",
                    "root",
                ).returncode,
                0,
            )
            self.assertEqual(run_cli(state_root, "dispatch-once").returncode, 0)
            self.assertEqual(json.loads(run_cli(state_root, "goals", "time").stdout)[0]["completion_state"], "complete")

            worker = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "cli",
                    "--root",
                    str(state_root),
                    "dispatch-worker",
                    "--max-dispatches",
                    "1",
                    "--idle-timeout",
                    "5",
                    "--interval",
                    "0.05",
                ],
                cwd=ROOT,
                env=ENV,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                time.sleep(0.15)
                user_input = run_cli(state_root, "user-input", "time", "root", "what time is it now?")
                self.assertEqual(user_input.returncode, 0, user_input.stderr)
                stdout, stderr = worker.communicate(timeout=10)
            finally:
                if worker.poll() is None:
                    worker.terminate()
                    worker.communicate(timeout=5)

            self.assertEqual(worker.returncode, 0, stderr)
            worker_payload = json.loads(stdout)
            self.assertEqual(worker_payload["dispatched_count"], 1)
            self.assertEqual(worker_payload["results"][0]["session"]["session_id"], "time")

            goals = json.loads(run_cli(state_root, "goals", "time").stdout)
            self.assertEqual(goals[0]["completion_state"], "complete")
            messages = json.loads(run_cli(state_root, "messages", "time").stdout)
            self.assertFalse(any("goal_completion_state" in message.get("payload", {}) for message in messages))
            self.assertTrue(any(message.get("payload", {}).get("user_input") for message in messages))

    def test_user_input_does_not_block_while_dispatch_agent_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            codex_path = bin_dir / "codex"
            codex_path.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "import time",
                        "time.sleep(1.0)",
                        "print('<aize-output>')",
                        "print('AIZE_GOAL_STATUS: completed')",
                        "print('AIZE_GOAL_REASON: slow worker finished')",
                        "print('</aize-output>')",
                    ]
                ),
                encoding="utf-8",
            )
            codex_path.chmod(0o755)
            env = {
                "PYTHONPATH": str(ROOT / "src"),
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            }

            self.assertEqual(run_cli_with_env(state_root, env, "init").returncode, 0)
            self.assertEqual(
                run_cli_with_env(
                    state_root,
                    env,
                    "start-goal",
                    "slow-session",
                    "--label",
                    "Slow",
                    "--created-by",
                    "root",
                ).returncode,
                0,
            )

            worker = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "cli",
                    "--root",
                    str(state_root),
                    "dispatch-worker",
                    "--session",
                    "slow-session",
                    "--max-dispatches",
                    "1",
                    "--idle-timeout",
                    "5",
                    "--interval",
                    "0.05",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                time.sleep(0.2)
                started = time.monotonic()
                user_input = run_cli_with_env(
                    state_root,
                    env,
                    "user-input",
                    "slow-session",
                    "root",
                    "second message while agent runs",
                )
                elapsed = time.monotonic() - started
                self.assertEqual(user_input.returncode, 0, user_input.stderr)
                self.assertLess(elapsed, 0.8)
                stdout, stderr = worker.communicate(timeout=10)
            finally:
                if worker.poll() is None:
                    worker.terminate()
                    worker.communicate(timeout=5)
            self.assertEqual(worker.returncode, 0, stderr)
            self.assertEqual(json.loads(stdout)["dispatched_count"], 1)

    def test_read_commands_do_not_rewrite_stable_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"

            self.assertEqual(run_cli(state_root, "init").returncode, 0)
            self.assertEqual(run_cli(state_root, "create-session", "time").returncode, 0)
            self.assertEqual(run_cli(state_root, "user-input", "time", "root", "hello").returncode, 0)
            self.assertEqual(run_cli(state_root, "status").returncode, 0)

            before = (state_root / "state.json").read_text(encoding="utf-8")
            self.assertEqual(run_cli(state_root, "status").returncode, 0)
            self.assertEqual(run_cli(state_root, "dispatch-index", "time").returncode, 0)
            after = (state_root / "state.json").read_text(encoding="utf-8")
            self.assertEqual(after, before)

    def test_goal_manager_incomplete_creates_implicit_worker_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            codex_path = bin_dir / "codex"
            codex_path.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "import sys",
                        "prompt = sys.argv[-1]",
                        "print('<aize-output role=\"GoalManager\" provider=\"codex\">')",
                        "if 'phase=\"review\"' in prompt:",
                        "    print('AIZE_GOAL_STATUS: incomplete')",
                        "    print('AIZE_GOAL_REASON: waiting for more work')",
                        "else:",
                        "    print('AIZE_GOAL_STATUS: ready')",
                        "    print('AIZE_GOAL_REASON: ready')",
                        "print('</aize-output>')",
                    ]
                ),
                encoding="utf-8",
            )
            codex_path.chmod(0o755)
            env = {
                "PYTHONPATH": str(ROOT / "src"),
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            }

            self.assertEqual(run_cli_with_env(state_root, env, "init").returncode, 0)
            self.assertEqual(
                run_cli_with_env(
                    state_root,
                    env,
                    "start-goal",
                    "retry-session",
                    "--label",
                    "Retry",
                    "--created-by",
                    "root",
                ).returncode,
                0,
            )
            dispatched = run_cli_with_env(state_root, env, "dispatch-once")
            self.assertEqual(dispatched.returncode, 0, dispatched.stderr)
            payload = json.loads(dispatched.stdout)
            self.assertEqual(payload["goal"]["completion_state"], "incomplete")
            self.assertEqual(payload["run"]["completion_state"], "incomplete")
            self.assertTrue(payload["message"]["payload"]["implicit_worker_request"])

            queue = json.loads(run_cli_with_env(state_root, env, "dispatch-index", "retry-session").stdout)
            worker_entries = [
                entry
                for entry in queue
                if entry.get("status") == "queued" and entry.get("role") == "WorkerAgent"
            ]
            self.assertEqual(len(worker_entries), 1)
            self.assertEqual(worker_entries[0]["priority"], 150)
            self.assertNotIn("available_after", worker_entries[0])
            self.assertIn("requires WorkerAgent work", worker_entries[0]["reason"])

            goals = json.loads(run_cli_with_env(state_root, env, "goals", "retry-session").stdout)
            self.assertIn("waiting for more work", goals[0]["completion_reason"])
            messages = json.loads(run_cli_with_env(state_root, env, "messages", "retry-session").stdout)
            worker_requests = [
                message
                for message in messages
                if message.get("to") == "session:retry-session"
                and message.get("payload", {}).get("worker_request") is True
            ]
            self.assertEqual(len(worker_requests), 1)
            self.assertTrue(worker_requests[0]["payload"]["implicit_worker_request"])
            self.assertIn("waiting for more work", worker_requests[0]["payload"]["body"])

    def test_incomplete_worker_report_then_goal_manager_completion_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            codex_path = bin_dir / "codex"
            codex_path.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "import os, sys",
                        "prompt = sys.argv[-1]",
                        "role = os.environ.get('AIZE_AGENT_ROLE')",
                        "if role == 'GoalManager':",
                        "    print('<aize-output role=\"GoalManager\" provider=\"codex\">')",
                        "    if 'WorkerAgent finished delegated work' in prompt:",
                        "        print('AIZE_GOAL_STATUS: completed')",
                        "        print('AIZE_GOAL_REASON: worker result satisfies the goal')",
                        "    else:",
                        "        print('AIZE_GOAL_STATUS: incomplete')",
                        "        print('AIZE_GOAL_REASON: implement the delegated work and report back to Session')",
                        "    print('</aize-output>')",
                        "elif role == 'WorkerAgent':",
                        "    from agent_api import send_session_message",
                        "    send_session_message('WorkerAgent finished delegated work.')",
                        "    print('<aize-output role=\"WorkerAgent\" provider=\"codex\">worker reported to Session</aize-output>')",
                        "else:",
                        "    print('<aize-output role=\"unknown\" provider=\"codex\">unexpected</aize-output>')",
                    ]
                ),
                encoding="utf-8",
            )
            codex_path.chmod(0o755)
            env = {
                "PYTHONPATH": str(ROOT / "src"),
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            }

            self.assertEqual(run_cli_with_env(state_root, env, "init").returncode, 0)
            self.assertEqual(
                run_cli_with_env(
                    state_root,
                    env,
                    "start-goal",
                    "cycle-session",
                    "--label",
                    "Cycle",
                    "--created-by",
                    "root",
                ).returncode,
                0,
            )

            first = json.loads(run_cli_with_env(state_root, env, "dispatch-once").stdout)
            self.assertEqual(first["run"]["role"], "GoalManager")
            self.assertEqual(first["goal"]["completion_state"], "incomplete")
            self.assertTrue(first["message"]["payload"]["implicit_worker_request"])

            second = json.loads(run_cli_with_env(state_root, env, "dispatch-once").stdout)
            self.assertEqual(second["run"]["role"], "WorkerAgent")

            third = json.loads(run_cli_with_env(state_root, env, "dispatch-once").stdout)
            self.assertEqual(third["run"]["role"], "GoalManager")
            self.assertEqual(third["goal"]["completion_state"], "complete")

            messages = json.loads(run_cli_with_env(state_root, env, "messages", "cycle-session", "--limit", "0").stdout)
            self.assertTrue(any(message.get("payload", {}).get("implicit_worker_request") for message in messages))
            self.assertTrue(any("WorkerAgent finished delegated work" in message.get("payload", {}).get("body", "") for message in messages))

    def test_goal_manager_review_prompt_includes_session_message_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            bin_dir = Path(tmp) / "bin"
            capture_path = Path(tmp) / "completion-prompt.txt"
            bin_dir.mkdir()
            codex_path = bin_dir / "codex"
            codex_path.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "import os",
                        "import sys",
                        "prompt = sys.argv[-1]",
                        "open(os.environ['AIZE_CAPTURE_PROMPT'], 'w', encoding='utf-8').write(prompt)",
                        "print('<aize-output role=\"GoalManager\" provider=\"codex\">')",
                        "print('AIZE_GOAL_STATUS: completed')",
                        "print('AIZE_GOAL_REASON: done')",
                        "print('</aize-output>')",
                    ]
                ),
                encoding="utf-8",
            )
            codex_path.chmod(0o755)
            env = {
                "PYTHONPATH": str(ROOT / "src"),
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                "AIZE_CAPTURE_PROMPT": str(capture_path),
            }

            self.assertEqual(run_cli_with_env(state_root, env, "init").returncode, 0)
            self.assertEqual(run_cli_with_env(state_root, env, "create-session", "time").returncode, 0)
            user_input = run_cli_with_env(
                state_root,
                env,
                "user-input",
                "time",
                "root",
                "are you ready?",
                "--reply-to",
                "console-test",
            )
            self.assertEqual(user_input.returncode, 0, user_input.stderr)

            dispatched = run_cli_with_env(state_root, env, "dispatch-once")
            self.assertEqual(dispatched.returncode, 0, dispatched.stderr)
            review_prompt = capture_path.read_text(encoding="utf-8")
            self.assertIn('phase="review"', review_prompt)
            self.assertIn("<session-messages>", review_prompt)
            self.assertIn("<dispatch-feed>", review_prompt)
            self.assertIn("console-test", review_prompt)
            self.assertIn("are you ready?", review_prompt)

    def test_dispatch_only_runs_active_incomplete_goals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"

            self.assertEqual(run_cli(state_root, "init").returncode, 0)
            self.assertEqual(run_cli(state_root, "create-unit", "worker").returncode, 0)
            self.assertEqual(
                run_cli(
                    state_root,
                    "start-goal",
                    "inactive-session",
                    "--unit",
                    "worker",
                    "--label",
                    "InactiveGoal",
                    "--created-by",
                    "root",
                ).returncode,
                0,
            )
            self.assertEqual(run_cli(state_root, "deactivate-session", "inactive-session").returncode, 0)

            inactive_dispatch = json.loads(run_cli(state_root, "dispatch-once").stdout)
            self.assertIsNone(inactive_dispatch["dispatched"])

            status = json.loads(run_cli(state_root, "status").stdout)
            self.assertEqual(status["inactive_session_count"], 1)
            self.assertEqual(status["incomplete_goal_count"], 1)
            self.assertEqual(status["active_incomplete_goal_count"], 0)

            self.assertEqual(run_cli(state_root, "activate-session", "inactive-session").returncode, 0)
            active_dispatch = json.loads(run_cli(state_root, "dispatch-once").stdout)
            self.assertEqual(active_dispatch["goal"]["completion_state"], "complete")

            status = json.loads(run_cli(state_root, "status").stdout)
            self.assertEqual(status["active_incomplete_goal_count"], 0)
            self.assertEqual(status["complete_goal_count"], 1)

    def test_provider_assignment_records_codex_without_external_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"

            self.assertEqual(run_cli(state_root, "init").returncode, 0)
            goal_manager = run_cli(state_root, "set-agent", "GoalManager", "codex")
            self.assertEqual(goal_manager.returncode, 0, goal_manager.stderr)
            worker_agent = run_cli(state_root, "set-agent", "WorkerAgent", "codex")
            self.assertEqual(worker_agent.returncode, 0, worker_agent.stderr)

            agents = json.loads(run_cli(state_root, "agents").stdout)
            self.assertEqual([agent["provider"] for agent in agents], ["codex", "codex"])

            pool = json.loads(run_cli(state_root, "agent-pool").stdout)
            self.assertEqual([role["role"] for role in pool["roles"]], ["GoalManager", "WorkerAgent"])
            self.assertEqual([role["allocated"] for role in pool["roles"]], [0, 0])

            self.assertEqual(run_cli(state_root, "create-unit", "worker").returncode, 0)
            self.assertEqual(
                run_cli(
                    state_root,
                    "start-goal",
                    "codex-session",
                    "--unit",
                    "worker",
                    "--label",
                    "CodexAssigned",
                    "--created-by",
                    "root",
                ).returncode,
                0,
            )

            dispatched = run_cli(state_root, "dispatch-once")
            self.assertEqual(dispatched.returncode, 0, dispatched.stderr)
            payload = json.loads(dispatched.stdout)
            self.assertEqual([step["provider"] for step in payload["run"]["steps"]], ["codex"])
            self.assertIn("external execution is disabled", payload["run"]["steps"][0]["output"])

            messages = json.loads(run_cli(state_root, "messages", "codex-session").stdout)
            self.assertEqual(messages, [])

    def test_codex_external_execution_uses_full_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from agents import AgentRunner

            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            cwd_path = Path(tmp) / "session-workspace"
            cwd_path.mkdir()
            log_path = Path(tmp) / "codex-args.json"
            codex_path = bin_dir / "codex"
            codex_path.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "import json, os, pathlib, sys",
                        "payload = {'argv': sys.argv[1:], 'cwd': os.getcwd(), 'env': {key: os.environ.get(key) for key in ['AIZE_STATE_ROOT', 'AIZE_SESSION_ID', 'AIZE_SESSION_WORKSPACE', 'AIZE_AGENT_ROLE', 'AIZE_RUN_ID']}}",
                        f"pathlib.Path({str(log_path)!r}).write_text(json.dumps(payload), encoding='utf-8')",
                        "print('codex ok')",
                    ]
                ),
                encoding="utf-8",
            )
            codex_path.chmod(0o755)
            old_path = os.environ.get("PATH", "")
            os.environ["PATH"] = f"{bin_dir}{os.pathsep}{old_path}"
            try:
                result = AgentRunner(external_enabled=True).run(
                    "codex",
                    role="WorkerAgent",
                    prompt="prompt",
                    resume_token="resume-1",
                    runtime_env={
                        "AIZE_STATE_ROOT": "state-root",
                        "AIZE_SESSION_ID": "session-1",
                        "AIZE_SESSION_WORKSPACE": str(cwd_path),
                        "AIZE_AGENT_ROLE": "WorkerAgent",
                        "AIZE_RUN_ID": "run-1",
                    },
                    cwd=cwd_path,
                )
            finally:
                os.environ["PATH"] = old_path
            self.assertEqual(result.output, "codex ok")
            payload = json.loads(log_path.read_text(encoding="utf-8"))
            argv = payload["argv"]
            self.assertEqual(argv[0], "exec")
            self.assertIn("--skip-git-repo-check", argv)
            self.assertIn("--sandbox", argv)
            self.assertIn("danger-full-access", argv)
            self.assertIn("--dangerously-bypass-approvals-and-sandbox", argv)
            self.assertIn("Resume durable AIZE agent thread: resume-1", argv[-1])
            self.assertEqual(payload["cwd"], str(cwd_path))
            self.assertEqual(payload["env"]["AIZE_STATE_ROOT"], "state-root")
            self.assertEqual(payload["env"]["AIZE_SESSION_ID"], "session-1")
            self.assertEqual(payload["env"]["AIZE_SESSION_WORKSPACE"], str(cwd_path))
            self.assertEqual(payload["env"]["AIZE_AGENT_ROLE"], "WorkerAgent")
            self.assertEqual(payload["env"]["AIZE_RUN_ID"], "run-1")

    def test_dispatch_runs_external_agent_in_session_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            log_path = Path(tmp) / "codex-cwd.json"
            codex_path = bin_dir / "codex"
            codex_path.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "import json, os, pathlib",
                        "payload = {'cwd': os.getcwd(), 'workspace': os.environ.get('AIZE_SESSION_WORKSPACE'), 'unit_workspace': os.environ.get('AIZE_UNIT_WORKSPACE'), 'session': os.environ.get('AIZE_SESSION_ID')}",
                        f"pathlib.Path({str(log_path)!r}).write_text(json.dumps(payload), encoding='utf-8')",
                        "print('<aize-output>')",
                        "print('AIZE_GOAL_STATUS: completed')",
                        "print('AIZE_GOAL_REASON: workspace verified')",
                        "print('</aize-output>')",
                    ]
                ),
                encoding="utf-8",
            )
            codex_path.chmod(0o755)
            env = {
                "PYTHONPATH": str(ROOT / "src"),
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            }

            self.assertEqual(run_cli_with_env(state_root, env, "init").returncode, 0)
            self.assertEqual(run_cli_with_env(state_root, env, "create-unit", "worker").returncode, 0)
            units = json.loads(run_cli_with_env(state_root, env, "units").stdout)
            worker_unit = next(unit for unit in units if unit["unit_id"] == "worker")
            created = run_cli_with_env(state_root, env, "create-session", "workspace-session", "--unit", "worker")
            self.assertEqual(created.returncode, 0, created.stderr)
            session = json.loads(created.stdout)
            self.assertEqual(
                run_cli_with_env(
                    state_root,
                    env,
                    "update-goal",
                    "workspace-session",
                    "Verify workspace",
                    "--created-by",
                    "root",
                ).returncode,
                0,
            )
            dispatched = run_cli_with_env(state_root, env, "dispatch-once")
            self.assertEqual(dispatched.returncode, 0, dispatched.stderr)

            payload = json.loads(log_path.read_text(encoding="utf-8"))
            expected_workspace = str(state_root / session["workspace_path"])
            self.assertEqual(payload["cwd"], expected_workspace)
            self.assertEqual(payload["workspace"], expected_workspace)
            self.assertEqual(payload["unit_workspace"], str(state_root / worker_unit["workspace_path"]))
            self.assertEqual(payload["session"], "workspace-session")

    def test_goal_manager_must_be_local_but_worker_can_be_remote_aize(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"

            self.assertEqual(run_cli(state_root, "init").returncode, 0)
            rejected = run_cli(state_root, "set-agent", "GoalManager", "remote-aize")
            self.assertEqual(rejected.returncode, 2)
            self.assertIn("GoalManager provider must be local", rejected.stderr)

            accepted = run_cli(state_root, "set-agent", "WorkerAgent", "remote-aize")
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            self.assertEqual(json.loads(accepted.stdout)["provider"], "remote-aize")

            self.assertEqual(run_cli(state_root, "create-unit", "worker").returncode, 0)
            self.assertEqual(
                run_cli(
                    state_root,
                    "start-goal",
                    "remote-worker-session",
                    "--unit",
                    "worker",
                    "--label",
                    "RemoteWorker",
                    "--created-by",
                    "root",
                ).returncode,
                0,
            )

            dispatched = run_cli(state_root, "dispatch-once")
            self.assertEqual(dispatched.returncode, 0, dispatched.stderr)
            payload = json.loads(dispatched.stdout)
            self.assertEqual(
                [step["phase"] for step in payload["run"]["steps"]],
                ["GoalManagerReview"],
            )
            self.assertEqual([step["provider"] for step in payload["run"]["steps"]], ["codex"])

            messages = json.loads(run_cli(state_root, "messages", "remote-worker-session").stdout)
            self.assertFalse([message for message in messages if message.get("to") == "node:remote-aize"])

    def test_goal_manager_message_to_worker_triggers_worker_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            codex_path = bin_dir / "codex"
            codex_path.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "import os",
                        "import sys",
                        "prompt = sys.argv[-1]",
                        "role = os.environ.get('AIZE_AGENT_ROLE')",
                        "if role == 'GoalManager' and 'phase=\"review\"' in prompt:",
                        "    from agent_api import send_worker_request",
                        "    send_worker_request('Please handle the implementation requested by the user.')",
                        "    print('<aize-output role=\"GoalManager\" provider=\"codex\">')",
                        "    print('AIZE_GOAL_STATUS: incomplete')",
                        "    print('AIZE_GOAL_REASON: delegated work to WorkerAgent')",
                        "    print('</aize-output>')",
                        "else:",
                        "    print('<aize-output role=\"unknown\" provider=\"codex\">unexpected</aize-output>')",
                    ]
                ),
                encoding="utf-8",
            )
            codex_path.chmod(0o755)
            env = {
                "PYTHONPATH": str(ROOT / "src"),
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            }

            self.assertEqual(run_cli_with_env(state_root, env, "init").returncode, 0)
            self.assertEqual(run_cli_with_env(state_root, env, "set-agent", "WorkerAgent", "remote-aize").returncode, 0)
            self.assertEqual(
                run_cli_with_env(
                    state_root,
                    env,
                    "start-goal",
                    "delegated-session",
                    "--label",
                    "Delegated",
                    "--created-by",
                    "root",
                ).returncode,
                0,
            )
            user_input = run_cli_with_env(
                state_root,
                env,
                "user-input",
                "delegated-session",
                "root",
                "please implement this through worker",
            )
            self.assertEqual(user_input.returncode, 0, user_input.stderr)

            dispatched = run_cli_with_env(state_root, env, "dispatch-once")
            self.assertEqual(dispatched.returncode, 0, dispatched.stderr)
            payload = json.loads(dispatched.stdout)
            self.assertEqual(
                [step["phase"] for step in payload["run"]["steps"]],
                ["GoalManagerReview"],
            )
            self.assertEqual([step["provider"] for step in payload["run"]["steps"]], ["codex"])

            worker_dispatched = run_cli_with_env(state_root, env, "dispatch-once")
            self.assertEqual(worker_dispatched.returncode, 0, worker_dispatched.stderr)
            worker_payload = json.loads(worker_dispatched.stdout)
            self.assertEqual(
                [step["phase"] for step in worker_payload["run"]["steps"]],
                ["WorkerWork", "RemoteAizeWorkerHandoff"],
            )
            self.assertEqual([step["provider"] for step in worker_payload["run"]["steps"]], ["remote-aize", "remote-aize"])

            messages = json.loads(run_cli_with_env(state_root, env, "messages", "delegated-session", "--limit", "0").stdout)
            worker_requests = [
                message
                for message in messages
                if message.get("to") == "session:delegated-session"
                and message.get("payload", {}).get("worker_request") is True
            ]
            self.assertEqual(len(worker_requests), 1)
            self.assertIn("Please handle the implementation", worker_requests[0]["payload"]["body"])
            handoffs = [message for message in messages if message.get("to") == "node:remote-aize"]
            self.assertEqual(len(handoffs), 1)
            worker_prompt = handoffs[0]["payload"]["remote_aize_worker_handoff"]["worker_prompt"]
            self.assertIn("please implement this through worker", worker_prompt)
            self.assertIn("Please handle the implementation", worker_prompt)
            self.assertIn("<dispatch-feed>", worker_prompt)
            self.assertIn("<worker_request>True</worker_request>", worker_prompt)

    def test_console_login_select_send_and_start_goal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"

            script = "\n".join(
                [
                    "create-unit worker",
                    "use root",
                    "session notes-session",
                    "use notes-session",
                    "current",
                    "use root",
                    "goal child-session BuildChild child body",
                    "send hello from console",
                    "dispatch",
                    "dispatch",
                    "use child-session",
                    "messages",
                    "goals",
                    "agent-threads",
                    "agent-pool",
                    "dispatch-runs",
                    "graph",
                    "exit",
                    "",
                ]
            )
            result = run_cli_with_input(
                state_root,
                script,
                "console",
                "--username",
                "root",
                "--password",
                "root",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("logged in as root", result.stdout)
            self.assertIn("Created goal session", result.stdout)
            self.assertIn("Created session", result.stdout)
            self.assertIn("session: notes-session unit=none", result.stdout)
            self.assertIn("session: child-session unit=none", result.stdout)
            self.assertIn("hello from console", result.stdout)
            self.assertIn("account:root -> session:root", result.stdout)
            self.assertIn("dispatch queued in background", result.stdout)
            self.assertIn("goal: child body [complete]", result.stdout)
            self.assertIn("No messages.", result.stdout)
            self.assertNotIn("agent:GoalManager -> agent:WorkerAgent", result.stdout)
            self.assertNotIn("agent:WorkerAgent -> agent:GoalManager", result.stdout)
            self.assertIn("Agent threads (1)", result.stdout)
            self.assertIn("- GoalManager session=child-session", result.stdout)
            self.assertNotIn("- WorkerAgent session=child-session", result.stdout)
            self.assertIn("Agent pool", result.stdout)
            self.assertIn("- GoalManager provider=codex status=active allocated=0", result.stdout)
            self.assertIn("- WorkerAgent provider=codex status=active allocated=0", result.stdout)
            self.assertIn("child-session [Active, Complete] G:0,W:0 unit=none", result.stdout)
            self.assertIn("notes-session [Active, NoGoal] G:0,W:0 unit=none", result.stdout)

    def test_console_send_dispatches_current_session_before_other_queued_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"

            self.assertEqual(run_cli(state_root, "init").returncode, 0)
            self.assertEqual(run_cli(state_root, "create-session", "other").returncode, 0)
            self.assertEqual(
                run_cli(
                    state_root,
                    "update-goal",
                    "other",
                    "Other",
                    "--created-by",
                    "root",
                ).returncode,
                0,
            )
            self.assertEqual(run_cli(state_root, "create-session", "time").returncode, 0)
            self.assertEqual(
                run_cli(
                    state_root,
                    "update-goal",
                    "time",
                    "Time",
                    "--created-by",
                    "root",
                ).returncode,
                0,
            )
            self.assertEqual(run_cli(state_root, "dispatch-once").returncode, 0)
            self.assertEqual(run_cli(state_root, "dispatch-once").returncode, 0)
            self.assertEqual(
                [goal["completion_state"] for goal in json.loads(run_cli(state_root, "goals").stdout)],
                ["complete", "complete"],
            )

            self.assertEqual(run_cli(state_root, "user-input", "other", "root", "older other input").returncode, 0)
            script = "\n".join(["use time", "send current time input", "goals", "exit", ""])
            result = run_cli_with_input(
                state_root,
                script,
                "console",
                "--username",
                "root",
                "--password",
                "root",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("session: time", result.stdout)
            self.assertIn("dispatch queued in background", result.stdout)

            time_goal = wait_for_goal_state(state_root, "time", "complete")
            other_goal = wait_for_goal_state(state_root, "other", "complete")
            self.assertEqual(time_goal["completion_state"], "complete")
            self.assertEqual(other_goal["completion_state"], "complete")

    def test_console_update_goal_uses_body_without_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"

            self.assertEqual(run_cli(state_root, "init").returncode, 0)
            self.assertEqual(run_cli(state_root, "create-session", "plain-goal").returncode, 0)
            script = "\n".join(
                [
                    "use plain-goal",
                    "update-goal reply to the user without a separate title",
                    "goals",
                    "exit",
                    "",
                ]
            )
            result = run_cli_with_input(
                state_root,
                script,
                "console",
                "--username",
                "root",
                "--password",
                "root",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("goal: reply to the user without a separate title", result.stdout)

            goals = json.loads(run_cli(state_root, "goals", "plain-goal").stdout)
            self.assertEqual(goals[0]["body"], "reply to the user without a separate title")
            self.assertEqual(goals[0]["completion_state"], "incomplete")

    def test_console_startup_dispatches_queued_active_incomplete_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"

            self.assertEqual(run_cli(state_root, "init").returncode, 0)
            self.assertEqual(run_cli(state_root, "create-session", "resume-me").returncode, 0)
            self.assertEqual(
                run_cli(
                    state_root,
                    "update-goal",
                    "resume-me",
                    "Resume queued work",
                    "--created-by",
                    "root",
                ).returncode,
                0,
            )

            result = run_cli_with_input(
                state_root,
                "exit\n",
                "console",
                "--username",
                "root",
                "--password",
                "root",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("startup dispatch worker queued", result.stdout)

            goal = wait_for_goal_state(state_root, "resume-me", "complete")
            self.assertEqual(goal["completion_state"], "complete")
            runs = json.loads(run_cli(state_root, "dispatch-runs", "resume-me").stdout)
            self.assertTrue(any("CLI console started or restarted" in run.get("recovery_context", "") for run in runs))


if __name__ == "__main__":
    unittest.main()
