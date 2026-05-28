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
    describe_session_template_schedule,
    ensure_auto_scheduled_root_unit_states,
    get_launchable_session_template,
    get_registered_unit_state,
    launch_session_template,
    list_launchable_session_templates,
    list_registered_session_template_states,
    list_registered_unit_states,
    resolve_session_template_launch_parent_session_id,
    update_registered_session_template_state,
)
from runtime.persistent_state_pkg import (
    create_conversation_session,
    ensure_state,
    get_session_settings,
    list_session_children,
    list_session_parents,
    load_session_skills,
    session_skill_file_path,
    update_session_goal_flags,
)


class SessionTemplateLauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plugin_dir = Path(tempfile.mkdtemp(prefix="test_launcher_", dir=ROOT / "plugins"))
        (self.plugin_dir / "plugin.json").write_text(
            json.dumps(
                {
                    "plugin_id": self.plugin_dir.name,
                    "display_name": "Launcher Plugin",
                    "catalog_visibility": "private",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        session_template_dir = self.plugin_dir / "units" / "research_launcher"
        session_template_dir.mkdir(parents=True, exist_ok=True)
        (session_template_dir / "unit.json").write_text(
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
                        "workspace_scope": "unit",
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
        communication_template_dir = self.plugin_dir / "units" / "communication_launcher"
        communication_template_dir.mkdir(parents=True, exist_ok=True)
        (communication_template_dir / "unit.json").write_text(
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
            templates = list_launchable_session_templates(default_provider="codex")
            template = get_launchable_session_template("research_launcher", default_provider="codex")

        self.assertTrue(any(item["template_id"] == "research_launcher" for item in templates))
        self.assertEqual(template["launcher"]["preferred_provider"], "claude")
        self.assertEqual(template["launcher"]["selected_agents"], ["claude_pool"])
        self.assertEqual(
            template["launcher"]["service_targets"],
            [{"mode": "pool", "provider": "claude", "target": "claude_pool"}],
        )
        self.assertEqual(template["launcher"]["workspace_scope"], "unit")
        self.assertEqual(template["launcher"]["schedule"]["timezone"], "America/New_York")
        self.assertEqual(template["launcher"]["schedule"]["daily_time"], "05:42")
        self.assertTrue(bool(template["launcher"]["schedule"]["enabled"]))

    def test_public_catalog_includes_entrance_and_development_units(self) -> None:
        with patch.dict("os.environ", {"AIZE_PLUGIN_ROOTS": str(ROOT / "plugins")}):
            public_templates = list_launchable_session_templates(
                default_provider="codex",
                include_private=False,
            )
            public_ids = {item["template_id"] for item in public_templates}
            development_unit = get_launchable_session_template(
                "aize-development.bug-hunting",
                default_provider="codex",
            )
            entrance_unit = get_launchable_session_template(
                "entrance.service",
                default_provider="codex",
                include_private=False,
            )

        self.assertIn("entrance.service", public_ids)
        self.assertIn("aize-development.bug-hunting", public_ids)
        self.assertEqual(development_unit["template_id"], "aize-development.bug-hunting")
        self.assertEqual(development_unit["display_name"], "AIze Development")
        self.assertEqual(development_unit["unit_class"], "service")
        self.assertEqual(development_unit["instance_policy"], "singleton")
        self.assertEqual(entrance_unit["template_id"], "entrance.service")

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
            self.assertEqual(stored["launcher_unit_id"], "research_launcher")
            self.assertEqual(stored["launcher_template_id"], "research_launcher")
            self.assertEqual(
                stored["launcher_service_targets"],
                [{"mode": "pool", "provider": "claude", "target": "claude_pool"}],
            )
            self.assertEqual(stored["launcher_workspace_scope"], "unit")
            workspace_path = Path(stored["launcher_workspace_path"])
            self.assertTrue(workspace_path.exists())
            self.assertTrue(workspace_path.is_dir())
            self.assertEqual(
                workspace_path,
                runtime_root / ".aize-state" / "units" / "repyt" / "research_launcher" / "workspace",
            )
            self.assertEqual(launched["launch_plan"]["workspace_scope"], "unit")
            self.assertEqual(launched["launch_plan"]["workspace_path"], str(workspace_path))
            self.assertIn(str(workspace_path), launched["launch_plan"]["initial_prompt"])
            self.assertIn("durable code, scripts, notes, and stock", launched["launch_plan"]["initial_prompt"])
            registered_templates = list_registered_session_template_states(runtime_root)
            template_state = next(item for item in registered_templates if item["template_id"] == "research_launcher")
            self.assertEqual(template_state["last_session_id"], str(session["session_id"]))
            self.assertEqual(template_state["last_parent_session_id"], str(parent["session_id"]))
            registered_units = list_registered_unit_states(runtime_root)
            unit_state = get_registered_unit_state(runtime_root, username="repyt", unit_id="research_launcher")
            self.assertTrue(any(item["unit_id"] == "research_launcher" for item in registered_units))
            self.assertIsNotNone(unit_state)
            assert unit_state is not None
            self.assertEqual(unit_state["last_session_id"], str(session["session_id"]))

    def test_aize_development_unit_reuses_singleton_session(self) -> None:
        with tempfile.TemporaryDirectory() as runtime_dir:
            runtime_root = Path(runtime_dir)
            ensure_state(runtime_root)
            entrance = create_conversation_session(runtime_root, username="repyt", label="Entrance")
            with patch.dict("os.environ", {"AIZE_PLUGIN_ROOTS": str(ROOT / "plugins")}):
                unit = get_launchable_session_template(
                    "aize-development.bug-hunting",
                    default_provider="codex",
                    include_private=False,
                )
                first = launch_session_template(
                    runtime_root,
                    username="repyt",
                    parent_session_id=str(entrance["session_id"]),
                    app=unit,
                )
                second = launch_session_template(
                    runtime_root,
                    username="repyt",
                    parent_session_id=str(entrance["session_id"]),
                    app=unit,
                )

            first_session_id = str(first["session"]["session_id"])
            second_session_id = str(second["session"]["session_id"])
            self.assertEqual(first_session_id, second_session_id)
            self.assertFalse(bool(first["launch_plan"].get("reused_existing_session")))
            self.assertTrue(bool(second["launch_plan"].get("reused_existing_session")))
            stored = get_session_settings(runtime_root, username="repyt", session_id=first_session_id)
            self.assertIsNotNone(stored)
            assert stored is not None
            self.assertEqual(stored["label"], "AIze Development")
            self.assertEqual(stored["launcher_unit_id"], "aize-development.bug-hunting")
            self.assertEqual(stored["launcher_unit_class"], "service")
            self.assertEqual(stored["launcher_instance_policy"], "singleton")
            unit_state = get_registered_unit_state(
                runtime_root,
                username="repyt",
                unit_id="aize-development.bug-hunting",
            )
            self.assertIsNotNone(unit_state)
            assert unit_state is not None
            self.assertEqual(unit_state["last_session_id"], first_session_id)

    def test_launch_session_template_reuses_template_workspace_across_sessions(self) -> None:
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

    def test_bug_hunting_unit_provisions_canonical_session_skills(self) -> None:
        with tempfile.TemporaryDirectory() as runtime_dir:
            runtime_root = Path(runtime_dir)
            ensure_state(runtime_root)
            parent = create_conversation_session(runtime_root, username="repyt", label="Parent")
            with patch.dict("os.environ", {"AIZE_PLUGIN_ROOTS": str(ROOT / "plugins")}):
                unit = get_launchable_session_template("aize-development.bug-hunting", default_provider="codex")
                launched = launch_session_template(
                    runtime_root,
                    username="repyt",
                    parent_session_id=str(parent["session_id"]),
                    app=unit,
                )

            self.assertEqual(unit["display_name"], "AIze Development")
            self.assertEqual(unit["unit_class"], "service")
            self.assertEqual(unit["instance_policy"], "singleton")
            self.assertEqual(unit["launcher"]["workspace_scope"], "unit")
            self.assertEqual(unit["launcher"]["resident_parent_session_id"], "default")
            self.assertEqual(unit["launcher"]["session_group"], "root")
            self.assertEqual(unit["launcher"]["selected_agents"], ["codex_pool"])
            self.assertIn("canonical non-UI AIzeDevelopment parent session", unit["launcher"]["goal_text"])
            self.assertIn("child of the Root session", unit["launcher"]["goal_text"])
            self.assertIn("constitutional objective", unit["launcher"]["goal_text"])
            self.assertIn("separate port or isolated runtime", unit["launcher"]["goal_text"])
            self.assertIn("stop-and-migrate", unit["launcher"]["goal_text"])
            self.assertIn("not parallel to Root", unit["launcher"]["initial_prompt"])
            self.assertIn("project-developer instruction", unit["launcher"]["initial_prompt"])
            self.assertIn("Treat Entrance as IO/management only", unit["launcher"]["initial_prompt"])
            self.assertEqual(unit["launcher"]["skills"][0]["canonical_session_key"], "aize.development")
            self.assertIn("persistent AIzeDevelopment parent workflow", unit["launcher"]["skills"][0]["description"])
            self.assertIn("Root-goal lineage", unit["launcher"]["skills"][0]["description"])
            self.assertIn("parent coordinator", unit["launcher"]["skills"][0]["when_to_use"])
            self.assertIn("subordinate subgoal", unit["launcher"]["skills"][0]["usage"])
            self.assertIn("Root session's overall goal", unit["launcher"]["skills"][0]["prompt"])
            self.assertIn("separate port or isolated runtime", unit["launcher"]["skills"][0]["usage"])
            self.assertIn("delegated child task", unit["launcher"]["skills"][0]["prompt"])
            self.assertEqual(unit["launcher"]["skills"][0]["files"][0]["path"], "README.md")
            self.assertEqual(launched["session"]["parent_session_id"], "default")
            self.assertEqual(launched["session"]["session_group"], "root")
            session_id = str(launched["session"]["session_id"])
            skills = load_session_skills(runtime_root, username="repyt", session_id=session_id)
            self.assertEqual(skills[0]["skill_id"], "aize-development-session")
            self.assertEqual(skills[0]["canonical_session_key"], "aize.development")
            self.assertEqual(skills[1]["skill_id"], "unit-file-migration-audit")
            self.assertIn(
                "separate port or isolated runtime",
                session_skill_file_path(
                    runtime_root,
                    username="repyt",
                    session_id=session_id,
                    relative_path="README.md",
                ).read_text(encoding="utf-8"),
            )
            self.assertIn(
                "canonical non-UI AIzeDevelopment parent workflow",
                session_skill_file_path(
                    runtime_root,
                    username="repyt",
                    session_id=session_id,
                    relative_path="README.md",
                ).read_text(encoding="utf-8"),
            )
            self.assertIn(
                "Root session's goal is the overall constitutional objective",
                session_skill_file_path(
                    runtime_root,
                    username="repyt",
                    session_id=session_id,
                    relative_path="README.md",
                ).read_text(encoding="utf-8"),
            )
            self.assertIn(
                "Stop the currently running AIze runtime",
                session_skill_file_path(
                    runtime_root,
                    username="repyt",
                    session_id=session_id,
                    relative_path="development-cycle.md",
                ).read_text(encoding="utf-8"),
            )
            self.assertIn(
                "subordinate subgoal",
                session_skill_file_path(
                    runtime_root,
                    username="repyt",
                    session_id=session_id,
                    relative_path="development-cycle.md",
                ).read_text(encoding="utf-8"),
            )
            self.assertIn(
                "compatibility inputs only",
                session_skill_file_path(
                    runtime_root,
                    username="repyt",
                    session_id=session_id,
                    relative_path="migration-audit.md",
                ).read_text(encoding="utf-8"),
            )

    def test_resolve_bug_hunting_parent_repairs_existing_root_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as runtime_dir:
            runtime_root = Path(runtime_dir)
            ensure_state(runtime_root)
            orphan_parent = create_conversation_session(
                runtime_root,
                username="repyt",
                label="AIze Development",
            )
            orphan_parent_session_id = str(orphan_parent["session_id"])
            with patch.dict("os.environ", {"AIZE_PLUGIN_ROOTS": str(ROOT / "plugins")}):
                unit = get_launchable_session_template(
                    "aize-development.bug-hunting",
                    default_provider="codex",
                )
            update_registered_session_template_state(
                runtime_root,
                username="repyt",
                template_id="aize-development.bug-hunting",
                updates={
                    "display_name": "AIze Bug Hunting",
                    "plugin_id": "aize-development",
                    "package_id": "aize-development",
                    "last_session_id": orphan_parent_session_id,
                    "last_parent_session_id": "",
                },
            )

            resolved_parent_session_id = resolve_session_template_launch_parent_session_id(
                runtime_root,
                username="repyt",
                template_state=get_registered_unit_state(
                    runtime_root,
                    username="repyt",
                    unit_id="aize-development.bug-hunting",
                ),
                session_template=unit,
            )

            self.assertEqual(resolved_parent_session_id, "default")
            repaired_parent = get_session_settings(
                runtime_root,
                username="repyt",
                session_id=orphan_parent_session_id,
            )
            self.assertIsNotNone(repaired_parent)
            assert repaired_parent is not None
            self.assertEqual(repaired_parent["parent_session_id"], "default")
            self.assertEqual(
                list_session_parents(
                    runtime_root,
                    username="repyt",
                    session_id=orphan_parent_session_id,
                ),
                ["default"],
            )
            self.assertIn(
                orphan_parent_session_id,
                list_session_children(
                    runtime_root,
                    username="repyt",
                    session_id="default",
                ),
            )
            refreshed_state = get_registered_unit_state(
                runtime_root,
                username="repyt",
                unit_id="aize-development.bug-hunting",
            )
            self.assertIsNotNone(refreshed_state)
            assert refreshed_state is not None
            self.assertEqual(refreshed_state["last_parent_session_id"], "default")

    def test_minix_refactor_unit_is_scheduled_development_child(self) -> None:
        with tempfile.TemporaryDirectory() as runtime_dir:
            runtime_root = Path(runtime_dir)
            ensure_state(runtime_root)
            entrance = create_conversation_session(runtime_root, username="repyt", label="Entrance")
            with patch.dict("os.environ", {"AIZE_PLUGIN_ROOTS": str(ROOT / "plugins")}):
                development_unit = get_launchable_session_template("aize-development.bug-hunting", default_provider="codex")
                development = launch_session_template(
                    runtime_root,
                    username="repyt",
                    parent_session_id=str(entrance["session_id"]),
                    app=development_unit,
                    label="AIze Development",
                )
                minix_unit = get_launchable_session_template("aize-development.minix-refactor", default_provider="codex")
                launched = launch_session_template(
                    runtime_root,
                    username="repyt",
                    parent_session_id=str(entrance["session_id"]),
                    app=minix_unit,
                )

            self.assertEqual(minix_unit["launcher"]["parent_unit_id"], "aize-development.bug-hunting")
            self.assertEqual(minix_unit["launcher"]["schedule"]["kind"], "interval")
            self.assertEqual(minix_unit["launcher"]["schedule"]["every_hours"], 4)
            self.assertEqual(minix_unit["launcher"]["child_session_sharing"]["mode"], "private")
            self.assertIn("child session", minix_unit["launcher"]["goal_text"].lower())
            self.assertIn("Entrance is IO/management only", minix_unit["launcher"]["initial_prompt"])
            self.assertIn("skill-created delegated sessions", minix_unit["launcher"]["initial_prompt"])
            self.assertIn("child session", minix_unit["launcher"]["initial_prompt"].lower())
            self.assertIn("verification", minix_unit["launcher"]["initial_prompt"].lower())
            self.assertIn("report", minix_unit["launcher"]["initial_prompt"].lower())
            self.assertEqual(
                launched["session"]["parent_session_id"],
                development["session"]["session_id"],
            )
            self.assertNotEqual(launched["session"]["parent_session_id"], entrance["session_id"])
            self.assertIn("Persistent unit workspace directory", launched["launch_plan"]["initial_prompt"])
            session_id = str(launched["session"]["session_id"])
            skills = load_session_skills(runtime_root, username="repyt", session_id=session_id)
            self.assertEqual(skills[0]["skill_id"], "aize-minix-incremental-refactor")
            self.assertEqual(skills[0]["canonical_session_key"], "aize.development.minix-refactor")
            self.assertIn("never inside Entrance", skills[0]["prompt"])
            self.assertIn(
                "Crawl Record",
                session_skill_file_path(
                    runtime_root,
                    username="repyt",
                    session_id=session_id,
                    relative_path="crawl-record.md",
                ).read_text(encoding="utf-8"),
            )
            self.assertIn(
                "Delegation Log",
                session_skill_file_path(
                    runtime_root,
                    username="repyt",
                    session_id=session_id,
                    relative_path="delegation-log.md",
                ).read_text(encoding="utf-8"),
            )
            self.assertIn(
                "spawn one narrow child session for implementation",
                session_skill_file_path(
                    runtime_root,
                    username="repyt",
                    session_id=session_id,
                    relative_path="README.md",
                ).read_text(encoding="utf-8"),
            )
            self.assertIn(
                "Entrance is IO/management only",
                session_skill_file_path(
                    runtime_root,
                    username="repyt",
                    session_id=session_id,
                    relative_path="README.md",
                ).read_text(encoding="utf-8"),
            )

    def test_system_diagnostics_unit_is_hourly_root_child(self) -> None:
        with tempfile.TemporaryDirectory() as runtime_dir:
            runtime_root = Path(runtime_dir)
            ensure_state(runtime_root)
            root = create_conversation_session(
                runtime_root,
                username="repyt",
                label="Root",
                session_group="root",
            )
            with patch.dict("os.environ", {"AIZE_PLUGIN_ROOTS": str(ROOT / "plugins")}):
                diagnostics_unit = get_launchable_session_template(
                    "aize-development.system-diagnostics",
                    default_provider="codex",
                )
                launched = launch_session_template(
                    runtime_root,
                    username="repyt",
                    parent_session_id=str(root["session_id"]),
                    app=diagnostics_unit,
                )

            self.assertEqual(diagnostics_unit["launcher"]["parent_unit_id"], "")
            self.assertEqual(diagnostics_unit["launcher"]["session_group"], "root")
            self.assertEqual(diagnostics_unit["lifecycle"], "auto")
            self.assertEqual(diagnostics_unit["launcher"]["schedule"]["kind"], "interval")
            self.assertEqual(diagnostics_unit["launcher"]["schedule"]["every_hours"], 1)
            self.assertEqual(launched["session"]["parent_session_id"], root["session_id"])
            self.assertEqual(launched["session"]["session_group"], "root")
            self.assertIn("Persistent unit workspace directory", launched["launch_plan"]["initial_prompt"])
            self.assertIn("pending-input queues", launched["launch_plan"]["initial_prompt"])
            self.assertIn("probe_restart.py", launched["launch_plan"]["initial_prompt"])
            session_id = str(launched["session"]["session_id"])
            skills = load_session_skills(runtime_root, username="repyt", session_id=session_id)
            self.assertEqual(skills[0]["skill_id"], "aize-system-diagnostics-hourly-audit")
            self.assertEqual(skills[0]["canonical_session_key"], "aize.system-diagnostics")
            self.assertIn(
                "Diagnostics Log",
                session_skill_file_path(
                    runtime_root,
                    username="repyt",
                    session_id=session_id,
                    relative_path="diagnostics-log.md",
                ).read_text(encoding="utf-8"),
            )

    def test_system_diagnostics_unit_auto_registers_for_root_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as runtime_dir:
            runtime_root = Path(runtime_dir)
            ensure_state(runtime_root)
            with patch.dict("os.environ", {"AIZE_PLUGIN_ROOTS": str(ROOT / "plugins")}):
                registered = ensure_auto_scheduled_root_unit_states(
                    runtime_root,
                    default_provider="codex",
                    username="root",
                )

            diagnostics = [
                record
                for record in registered
                if str(record.get("unit_id") or record.get("template_id") or "") == "aize-development.system-diagnostics"
            ]
            self.assertEqual(len(diagnostics), 1)
            self.assertEqual(diagnostics[0]["last_parent_session_id"], "default")
            self.assertIsNotNone(get_session_settings(runtime_root, username="root", session_id="default"))

    def test_system_monitor_unit_is_hourly_root_child(self) -> None:
        with tempfile.TemporaryDirectory() as runtime_dir:
            runtime_root = Path(runtime_dir)
            ensure_state(runtime_root)
            root = create_conversation_session(
                runtime_root,
                username="repyt",
                label="Root",
                session_group="root",
            )
            with patch.dict("os.environ", {"AIZE_PLUGIN_ROOTS": str(ROOT / "plugins")}):
                monitor_unit = get_launchable_session_template(
                    "aize-development.system-monitor",
                    default_provider="codex",
                )
                launched = launch_session_template(
                    runtime_root,
                    username="repyt",
                    parent_session_id=str(root["session_id"]),
                    app=monitor_unit,
                )

            self.assertEqual(monitor_unit["launcher"]["parent_unit_id"], "")
            self.assertEqual(monitor_unit["launcher"]["session_group"], "root")
            self.assertEqual(monitor_unit["launcher"]["schedule"]["kind"], "interval")
            self.assertEqual(monitor_unit["launcher"]["schedule"]["every_hours"], 1)
            self.assertEqual(launched["session"]["parent_session_id"], root["session_id"])
            self.assertEqual(launched["session"]["session_group"], "root")
            self.assertIn("runtime.system_monitor", launched["launch_plan"]["initial_prompt"])
            self.assertIn("probe_restart.py", launched["launch_plan"]["initial_prompt"])
            session_id = str(launched["session"]["session_id"])
            skills = load_session_skills(runtime_root, username="repyt", session_id=session_id)
            self.assertEqual(skills[0]["skill_id"], "aize-system-monitor")
            self.assertEqual(skills[0]["canonical_session_key"], "aize.system-monitor")
            self.assertTrue(skills[0]["files"][0]["preserve_existing"])
            self.assertIn(
                "AIze System Monitor",
                session_skill_file_path(
                    runtime_root,
                    username="repyt",
                    session_id=session_id,
                    relative_path="monitor-record.md",
                ).read_text(encoding="utf-8"),
            )

    def test_system_monitor_unit_auto_registers_for_root_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as runtime_dir:
            runtime_root = Path(runtime_dir)
            ensure_state(runtime_root)
            with patch.dict("os.environ", {"AIZE_PLUGIN_ROOTS": str(ROOT / "plugins")}):
                registered = ensure_auto_scheduled_root_unit_states(
                    runtime_root,
                    default_provider="codex",
                    username="root",
                )

            monitor = [
                record
                for record in registered
                if str(record.get("unit_id") or record.get("template_id") or "") == "aize-development.system-monitor"
            ]
            self.assertEqual(len(monitor), 1)
            self.assertEqual(monitor[0]["last_parent_session_id"], "default")
            self.assertIsNotNone(get_session_settings(runtime_root, username="root", session_id="default"))

    def test_interval_schedule_marks_due_on_four_hour_cadence(self) -> None:
        app = {
            "unit_id": "aize-development.minix-refactor",
            "launcher": {
                "schedule": {
                    "enabled": True,
                    "kind": "interval",
                    "every_hours": 4,
                    "timezone": "UTC",
                }
            },
        }
        template_state = {
            "created_at": "2026-04-20T08:00:00Z",
            "schedule_state": {},
        }
        schedule_info = describe_session_template_schedule(
            app,
            template_state=template_state,
            now=datetime(2026, 4, 20, 12, 0, tzinfo=UTC),
        )
        self.assertTrue(bool(schedule_info["due"]))
        self.assertEqual(schedule_info["scheduled_for_utc"], "2026-04-20T12:00:00Z")
        self.assertEqual(schedule_info["next_due_at"], "2026-04-20T16:00:00Z")

        template_state["schedule_state"] = {"last_triggered_occurrence_at": "2026-04-20T12:00:00Z"}
        schedule_info = describe_session_template_schedule(
            app,
            template_state=template_state,
            now=datetime(2026, 4, 20, 13, 0, tzinfo=UTC),
        )
        self.assertFalse(bool(schedule_info["due"]))
        self.assertEqual(schedule_info["scheduled_for_utc"], "2026-04-20T16:00:00Z")

    def test_scheduled_minix_refactor_resolves_registered_development_parent(self) -> None:
        with tempfile.TemporaryDirectory() as runtime_dir:
            runtime_root = Path(runtime_dir)
            ensure_state(runtime_root)
            entrance = create_conversation_session(runtime_root, username="repyt", label="Entrance")
            with patch.dict("os.environ", {"AIZE_PLUGIN_ROOTS": str(ROOT / "plugins")}):
                development_unit = get_launchable_session_template("aize-development.bug-hunting", default_provider="codex")
                development = launch_session_template(
                    runtime_root,
                    username="repyt",
                    parent_session_id=str(entrance["session_id"]),
                    app=development_unit,
                    label="AIze Development",
                )
                minix_unit = get_launchable_session_template("aize-development.minix-refactor", default_provider="codex")

            update_registered_session_template_state(
                runtime_root,
                username="repyt",
                template_id="aize-development.minix-refactor",
                updates={
                    "created_at": "2026-04-20T08:00:00Z",
                    "schedule_state": {},
                },
            )
            parent_session_id = resolve_session_template_launch_parent_session_id(
                runtime_root,
                username="repyt",
                template_state=get_registered_unit_state(
                    runtime_root,
                    username="repyt",
                    unit_id="aize-development.minix-refactor",
                ),
                session_template=minix_unit,
            )
            self.assertEqual(parent_session_id, development["session"]["session_id"])

    def test_entrance_unit_provisions_code_based_interactive_skill(self) -> None:
        with tempfile.TemporaryDirectory() as runtime_dir:
            runtime_root = Path(runtime_dir)
            ensure_state(runtime_root)
            parent = create_conversation_session(runtime_root, username="repyt", label="Parent")
            with patch.dict("os.environ", {"AIZE_PLUGIN_ROOTS": str(ROOT / "plugins")}):
                unit = get_launchable_session_template("entrance.service", default_provider="codex")
                launched = launch_session_template(
                    runtime_root,
                    username="repyt",
                    parent_session_id=str(parent["session_id"]),
                    app=unit,
                )

            session_id = str(launched["session"]["session_id"])
            skills = load_session_skills(runtime_root, username="repyt", session_id=session_id)
            lightweight = next(skill for skill in skills if skill["skill_id"] == "entrance-lightweight-response")
            self.assertEqual(lightweight["kind"], "interactive")
            self.assertEqual(lightweight["routing_mode"], "handle_user_message")
            self.assertEqual(lightweight["handler_file"], "entrance_lightweight_response.py")
            self.assertEqual(unit["instance_policy"], "multi")
            stored = get_session_settings(runtime_root, username="repyt", session_id=session_id)
            self.assertIsNotNone(stored)
            assert stored is not None
            self.assertEqual(stored["launcher_unit_id"], "entrance.service")
            self.assertEqual(stored["launcher_unit_kind"], "interface")
            self.assertEqual(stored["launcher_unit_class"], "service")
            self.assertEqual(stored["launcher_instance_policy"], "multi")
            self.assertIn("conversation and coordination layer", unit["launcher"]["goal_text"])
            self.assertIn("belongs in this conversation first", unit["launcher"]["initial_prompt"])
            self.assertNotIn(
                "canonical-development-routing",
                {skill["skill_id"] for skill in unit["launcher"]["skills"]},
            )
            self.assertNotIn("aize-development", json.dumps(unit, sort_keys=True))

    def test_entrance_unit_launch_supports_multiple_instances(self) -> None:
        with tempfile.TemporaryDirectory() as runtime_dir:
            runtime_root = Path(runtime_dir)
            ensure_state(runtime_root)
            parent = create_conversation_session(runtime_root, username="repyt", label="Parent")
            with patch.dict("os.environ", {"AIZE_PLUGIN_ROOTS": str(ROOT / "plugins")}):
                unit = get_launchable_session_template("entrance.service", default_provider="codex")
                first = launch_session_template(
                    runtime_root,
                    username="repyt",
                    parent_session_id=str(parent["session_id"]),
                    app=unit,
                    label="Entrance A",
                )
                second = launch_session_template(
                    runtime_root,
                    username="repyt",
                    parent_session_id=str(parent["session_id"]),
                    app=unit,
                    label="Entrance B",
                )

            first_session_id = str(first["session"]["session_id"])
            second_session_id = str(second["session"]["session_id"])
            self.assertNotEqual(first_session_id, second_session_id)
            first_stored = get_session_settings(runtime_root, username="repyt", session_id=first_session_id)
            second_stored = get_session_settings(runtime_root, username="repyt", session_id=second_session_id)
            self.assertIsNotNone(first_stored)
            self.assertIsNotNone(second_stored)
            assert first_stored is not None
            assert second_stored is not None
            self.assertEqual(first_stored["launcher_unit_id"], "entrance.service")
            self.assertEqual(second_stored["launcher_unit_id"], "entrance.service")
            self.assertEqual(first_stored["launcher_instance_policy"], "multi")
            self.assertEqual(second_stored["launcher_instance_policy"], "multi")
            self.assertNotIn(
                "canonical-development-routing",
                {skill["skill_id"] for skill in unit["launcher"]["skills"]},
            )
            self.assertIn(
                "def handle(context):",
                session_skill_file_path(
                    runtime_root,
                    username="repyt",
                    session_id=first_session_id,
                    relative_path="entrance_lightweight_response.py",
                ).read_text(encoding="utf-8"),
            )

    def test_describe_session_template_schedule_marks_due_once_per_occurrence(self) -> None:
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
            registered_templates = list_registered_session_template_states(runtime_root)
            template_state = next(item for item in registered_templates if item["template_id"] == "research_launcher")
            schedule_info = describe_session_template_schedule(app, template_state=template_state, now=now)
            self.assertTrue(bool(schedule_info["due"]))
            self.assertTrue(bool(schedule_info["template_registered"]))
            self.assertEqual(schedule_info["scheduled_for_utc"], "2026-04-20T09:42:00Z")
            self.assertEqual(schedule_info["next_due_at"], "2026-04-21T09:42:00Z")

            template_state["schedule_state"] = {"last_triggered_occurrence_at": "2026-04-20T09:42:00Z"}
            schedule_info = describe_session_template_schedule(app, template_state=template_state, now=now)
            self.assertFalse(bool(schedule_info["due"]))

    def test_describe_app_schedule_alias_remains_compatible(self) -> None:
        schedule_info = describe_app_schedule(
            {"launcher": {"schedule": {"enabled": False}}},
            app_state={"schedule_state": {}},
            now=datetime(2026, 4, 20, 9, 42, tzinfo=UTC),
        )

        self.assertTrue(bool(schedule_info["unit_registered"]))
        self.assertTrue(bool(schedule_info["app_registered"]))

if __name__ == "__main__":
    unittest.main()
