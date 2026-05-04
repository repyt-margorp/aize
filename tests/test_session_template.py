from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from session_template import (
    describe_app_schedule,
    get_launchable_session_template,
    launch_session_template,
    list_launchable_session_templates,
    list_registered_session_template_states,
)
from runtime.persistent_state_pkg import (
    create_conversation_session,
    ensure_state,
    get_session_settings,
    update_session_goal_flags,
)


class AppLauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plugin_dir = Path(tempfile.mkdtemp(prefix="test_launcher_", dir=ROOT / "plugins"))
        (self.plugin_dir / "plugin.json").write_text(
            json.dumps({"plugin_id": self.plugin_dir.name, "display_name": "Launcher Plugin"}) + "\n",
            encoding="utf-8",
        )
        session_template_dir = self.plugin_dir / "apps" / "research_launcher"
        session_template_dir.mkdir(parents=True, exist_ok=True)
        (session_template_dir / "session-template.json").write_text(
            json.dumps(
                {
                    "template_id": "research_launcher",
                    "display_name": "Research Launcher",
                    "description": "Spawn a research session",
                    "launcher": {
                        "default_label": "Research Sprint",
                        "goal_text": "Investigate the current topic",
                        "initial_prompt": "Start by outlining the research plan.",
                        "preferred_provider": "claude",
                        "selected_agents": ["claude_pool"],
                        "session_group": "user",
                        "workspace_scope": "app",
                        "schedule": {
                            "enabled": True,
                            "kind": "daily",
                            "timezone": "America/New_York",
                            "daily_time": "05:42",
                        },
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        communication_template_dir = self.plugin_dir / "apps" / "communication_launcher"
        communication_template_dir.mkdir(parents=True, exist_ok=True)
        (communication_template_dir / "session-template.json").write_text(
            json.dumps(
                {
                    "template_id": "communication_launcher",
                    "display_name": "Communication Launcher",
                    "description": "Spawn an interactive communication session",
                    "communication": {
                        "enabled": True,
                        "agent_priority": [
                            {
                                "provider": "codex",
                                "profile": "interactive-fast",
                                "model": "gpt-5.4-mini",
                                "session_mode": "ephemeral",
                                "ephemeral": True,
                                "config": {
                                    "model_reasoning_effort": "low",
                                },
                            },
                            "gemini",
                        ],
                    },
                    "launcher": {
                        "default_label": "Communication",
                        "goal_text": "Route user dialogue.",
                        "initial_prompt": "Respond quickly to the user and route work when needed.",
                        "preferred_provider": "claude",
                        "selected_agents": ["claude_pool"],
                        "session_group": "user",
                        "session_ui_mode": "communication",
                        "session_interactive": True,
                        "communication_agent_enabled": True,
                        "goal_completion_policy": "continuous",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.plugin_dir)

    def test_catalog_returns_launch_plan(self) -> None:
        with patch.dict("os.environ", {"AIZE_PLUGIN_ROOTS": str(ROOT / "plugins")}):
            apps = list_launchable_session_templates(default_provider="codex")
            app = get_launchable_session_template("research_launcher", default_provider="codex")

        self.assertTrue(any(item["template_id"] == "research_launcher" for item in apps))
        self.assertEqual(app["launcher"]["preferred_provider"], "claude")
        self.assertEqual(app["launcher"]["selected_agents"], ["claude_pool"])
        self.assertEqual(
            app["launcher"]["service_targets"],
            [{"mode": "pool", "provider": "claude", "target": "claude_pool"}],
        )
        self.assertEqual(app["launcher"]["workspace_scope"], "app")
        self.assertEqual(app["launcher"]["schedule"]["timezone"], "America/New_York")
        self.assertEqual(app["launcher"]["schedule"]["daily_time"], "05:42")
        self.assertTrue(bool(app["launcher"]["schedule"]["enabled"]))

    def test_launch_session_template_creates_configured_child_session(self) -> None:
        with tempfile.TemporaryDirectory() as runtime_dir:
            runtime_root = Path(runtime_dir)
            ensure_state(runtime_root)
            parent = create_conversation_session(runtime_root, username="repyt", label="Parent")
            with patch.dict("os.environ", {"AIZE_PLUGIN_ROOTS": str(ROOT / "plugins")}):
                app = get_launchable_session_template("research_launcher", default_provider="codex")
                launched = launch_session_template(
                    runtime_root,
                    username="repyt",
                    parent_session_id=str(parent["session_id"]),
                    app=app,
                    label="Launched Research",
                    goal_text="Collect private feature requirements",
                )

            session = launched["session"]
            stored = get_session_settings(runtime_root, username="repyt", session_id=str(session["session_id"]))
            self.assertIsNotNone(stored)
            self.assertEqual(stored["label"], "Launched Research")
            self.assertEqual(stored["goal_text"], "Collect private feature requirements")
            self.assertEqual(stored["preferred_provider"], "claude")
            self.assertEqual(stored["selected_agents"], ["claude_pool"])
            self.assertEqual(stored["launcher_template_id"], "research_launcher")
            self.assertEqual(
                stored["launcher_service_targets"],
                [{"mode": "pool", "provider": "claude", "target": "claude_pool"}],
            )
            self.assertEqual(stored["launcher_workspace_scope"], "app")
            workspace_path = Path(stored["launcher_workspace_path"])
            self.assertTrue(workspace_path.exists())
            self.assertTrue(workspace_path.is_dir())
            self.assertEqual(
                workspace_path,
                runtime_root / ".aize-state" / "apps" / "repyt" / "research_launcher" / "workspace",
            )
            self.assertEqual(launched["launch_plan"]["workspace_scope"], "app")
            self.assertEqual(launched["launch_plan"]["workspace_path"], str(workspace_path))
            self.assertIn(str(workspace_path), launched["launch_plan"]["initial_prompt"])
            self.assertIn("durable code, scripts, notes, and stock", launched["launch_plan"]["initial_prompt"])
            registered_apps = list_registered_session_template_states(runtime_root)
            app_state = next(item for item in registered_apps if item["template_id"] == "research_launcher")
            self.assertEqual(app_state["last_session_id"], str(session["session_id"]))
            self.assertEqual(app_state["last_parent_session_id"], str(parent["session_id"]))

    def test_launch_session_template_reuses_app_workspace_across_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as runtime_dir:
            runtime_root = Path(runtime_dir)
            ensure_state(runtime_root)
            parent = create_conversation_session(runtime_root, username="repyt", label="Parent")
            with patch.dict("os.environ", {"AIZE_PLUGIN_ROOTS": str(ROOT / "plugins")}):
                app = get_launchable_session_template("research_launcher", default_provider="codex")
                first = launch_session_template(
                    runtime_root,
                    username="repyt",
                    parent_session_id=str(parent["session_id"]),
                    app=app,
                )
                second = launch_session_template(
                    runtime_root,
                    username="repyt",
                    parent_session_id=str(parent["session_id"]),
                    app=app,
                    label="Second Launch",
                )

            self.assertEqual(
                first["launch_plan"]["workspace_path"],
                second["launch_plan"]["workspace_path"],
            )

    def test_launch_session_template_persists_interactive_communication_config(self) -> None:
        with tempfile.TemporaryDirectory() as runtime_dir:
            runtime_root = Path(runtime_dir)
            ensure_state(runtime_root)
            parent = create_conversation_session(runtime_root, username="repyt", label="Parent")
            with patch.dict("os.environ", {"AIZE_PLUGIN_ROOTS": str(ROOT / "plugins")}):
                app = get_launchable_session_template("communication_launcher", default_provider="codex")
                launched = launch_session_template(
                    runtime_root,
                    username="repyt",
                    parent_session_id=str(parent["session_id"]),
                    app=app,
                )

            self.assertEqual(app["launcher"]["session_ui_mode"], "communication")
            self.assertTrue(app["launcher"]["session_interactive"])
            self.assertTrue(app["launcher"]["communication_agent_enabled"])
            self.assertEqual(app["launcher"]["communication_agent_priority"][0]["provider"], "codex")
            self.assertEqual(app["launcher"]["communication_agent_priority"][0]["profile"], "interactive-fast")
            self.assertEqual(app["launcher"]["communication_agent_priority"][0]["session_slot"], "interactive_agent")
            self.assertEqual(app["launcher"]["communication_agent_priority"][0]["session_mode"], "ephemeral")
            self.assertTrue(app["launcher"]["communication_agent_priority"][0]["ephemeral"])
            session = launched["session"]
            stored = get_session_settings(runtime_root, username="repyt", session_id=str(session["session_id"]))
            self.assertIsNotNone(stored)
            self.assertEqual(stored["session_ui_mode"], "communication")
            self.assertTrue(stored["session_interactive"])
            self.assertTrue(stored["communication_agent_enabled"])
            self.assertEqual(stored["communication_agent_priority"][0]["provider"], "codex")
            self.assertEqual(stored["communication_agent_priority"][0]["session_slot"], "interactive_agent")
            self.assertEqual(stored["communication_agent_priority"][0]["session_mode"], "ephemeral")
            self.assertTrue(stored["communication_agent_priority"][0]["ephemeral"])
            self.assertEqual(stored["communication_agent_priority"][0]["config"]["model_reasoning_effort"], "low")
            self.assertEqual(stored["goal_completion_policy"], "continuous")
            self.assertFalse(stored["goal_completed"])
            update_session_goal_flags(
                runtime_root,
                username="repyt",
                session_id=str(session["session_id"]),
                goal_completed=True,
                goal_progress_state="complete",
            )
            refreshed = get_session_settings(runtime_root, username="repyt", session_id=str(session["session_id"]))
            self.assertIsNotNone(refreshed)
            assert refreshed is not None
            self.assertTrue(refreshed["goal_active"])
            self.assertFalse(refreshed["goal_completed"])
            self.assertEqual(refreshed["goal_progress_state"], "in_progress")

    def test_describe_app_schedule_marks_due_once_per_occurrence(self) -> None:
        with tempfile.TemporaryDirectory() as runtime_dir:
            runtime_root = Path(runtime_dir)
            ensure_state(runtime_root)
            parent = create_conversation_session(runtime_root, username="repyt", label="Parent")
            with patch.dict("os.environ", {"AIZE_PLUGIN_ROOTS": str(ROOT / "plugins")}):
                app = get_launchable_session_template("research_launcher", default_provider="codex")
                launched = launch_session_template(
                    runtime_root,
                    username="repyt",
                    parent_session_id=str(parent["session_id"]),
                    app=app,
                )

            now = datetime(2026, 4, 20, 9, 42, tzinfo=UTC)
            registered_apps = list_registered_session_template_states(runtime_root)
            app_state = next(item for item in registered_apps if item["template_id"] == "research_launcher")
            schedule_info = describe_app_schedule(app, app_state=app_state, now=now)
            self.assertTrue(bool(schedule_info["due"]))
            self.assertEqual(schedule_info["scheduled_for_utc"], "2026-04-20T09:42:00Z")
            self.assertEqual(schedule_info["next_due_at"], "2026-04-21T09:42:00Z")

            app_state["schedule_state"] = {"last_triggered_occurrence_at": "2026-04-20T09:42:00Z"}
            schedule_info = describe_app_schedule(app, app_state=app_state, now=now)
            self.assertFalse(bool(schedule_info["due"]))

if __name__ == "__main__":
    unittest.main()
