from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from app_launcher import get_launchable_app, launch_app_session, list_launchable_apps
from runtime.persistent_state import create_conversation_session, ensure_state, get_session_settings


class AppLauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plugin_dir = Path(tempfile.mkdtemp(prefix="test_launcher_", dir=ROOT / "plugins"))
        (self.plugin_dir / "plugin.json").write_text(
            json.dumps({"plugin_id": self.plugin_dir.name, "display_name": "Launcher Plugin"}) + "\n",
            encoding="utf-8",
        )
        app_dir = self.plugin_dir / "apps" / "research_launcher"
        app_dir.mkdir(parents=True, exist_ok=True)
        (app_dir / "app.json").write_text(
            json.dumps(
                {
                    "app_id": "research_launcher",
                    "display_name": "Research Launcher",
                    "description": "Spawn a research session",
                    "launcher": {
                        "default_label": "Research Sprint",
                        "goal_text": "Investigate the current topic",
                        "initial_prompt": "Start by outlining the research plan.",
                        "preferred_provider": "claude",
                        "selected_agents": ["claude_pool"],
                        "session_group": "user",
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
            apps = list_launchable_apps(default_provider="codex")
            app = get_launchable_app("research_launcher", default_provider="codex")

        self.assertTrue(any(item["app_id"] == "research_launcher" for item in apps))
        self.assertEqual(app["launcher"]["preferred_provider"], "claude")
        self.assertEqual(app["launcher"]["selected_agents"], ["claude_pool"])
        self.assertEqual(
            app["launcher"]["service_targets"],
            [{"mode": "pool", "provider": "claude", "target": "claude_pool"}],
        )

    def test_launch_app_session_creates_configured_child_session(self) -> None:
        with tempfile.TemporaryDirectory() as runtime_dir:
            runtime_root = Path(runtime_dir)
            ensure_state(runtime_root)
            parent = create_conversation_session(runtime_root, username="repyt", label="Parent")
            with patch.dict("os.environ", {"AIZE_PLUGIN_ROOTS": str(ROOT / "plugins")}):
                app = get_launchable_app("research_launcher", default_provider="codex")
                launched = launch_app_session(
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
            self.assertEqual(stored["launcher_app_id"], "research_launcher")
            self.assertEqual(
                stored["launcher_service_targets"],
                [{"mode": "pool", "provider": "claude", "target": "claude_pool"}],
            )
            self.assertEqual(launched["launch_plan"]["initial_prompt"], "Start by outlining the research plan.")


class AppLauncherSourceTests(unittest.TestCase):
    def test_http_and_ui_sources_expose_launcher_flow(self) -> None:
        http_source = (SRC / "runtime" / "http_handler.py").read_text(encoding="utf-8")
        html_source = (SRC / "runtime" / "html_renderer.py").read_text(encoding="utf-8")
        self.assertIn('if path == "/apps":', http_source)
        self.assertIn('if path == "/apps/launch":', http_source)
        self.assertIn("fetch(`/apps?", html_source)
        self.assertIn("fetch('/apps/launch'", html_source)
        self.assertIn("app-launcher-list", html_source)


if __name__ == "__main__":
    unittest.main()
