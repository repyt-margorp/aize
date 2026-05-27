from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from runtime.persistent_state_pkg import (
    append_history,
    create_child_conversation_session,
    create_conversation_session,
    get_history,
    load_session_skills,
    normalize_child_session_sharing_policy,
    session_skill_file_path,
    session_skills_manifest_path,
    update_session_skills,
)
from runtime.session_skills import (
    _skill_scope,
    append_session_skill_agent_turn,
    matching_interactive_session_skills,
    run_interactive_session_skill,
)
from runtime.ui_history import build_session_ui_history


class SessionSkillsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.runtime_root = Path(self.tempdir.name) / "runtime"
        self.username = "test-user"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_update_session_skills_persists_manifest_and_files(self) -> None:
        session = create_conversation_session(self.runtime_root, username=self.username, label="Root")
        session_id = str(session["session_id"])

        updated = update_session_skills(
            self.runtime_root,
            username=self.username,
            session_id=session_id,
            session_skills=[
                {
                    "skill_id": "canonical-dev",
                    "skill_scope": "adaptive",
                    "title": "Canonical Dev",
                    "canonical_session_key": "aize.development",
                    "files": [
                        {
                            "path": "README.md",
                            "content": "Implement routed changes here.",
                        }
                    ],
                }
            ],
        )

        self.assertIsNotNone(updated)
        manifest = session_skills_manifest_path(
            self.runtime_root,
            username=self.username,
            session_id=session_id,
        )
        self.assertTrue(manifest.exists())
        self.assertEqual(
            load_session_skills(
                self.runtime_root,
                username=self.username,
                session_id=session_id,
            )[0]["canonical_session_key"],
            "aize.development",
        )
        self.assertEqual(
            load_session_skills(
                self.runtime_root,
                username=self.username,
                session_id=session_id,
            )[0]["skill_scope"],
            "adaptive",
        )
        self.assertEqual(
            session_skill_file_path(
                self.runtime_root,
                username=self.username,
                session_id=session_id,
                relative_path="README.md",
            ).read_text(encoding="utf-8"),
            "Implement routed changes here.",
        )

    def test_update_session_skills_preserves_existing_durable_files(self) -> None:
        session = create_conversation_session(self.runtime_root, username=self.username, label="Root")
        session_id = str(session["session_id"])
        skill = {
            "skill_id": "monitor",
            "files": [
                {
                    "path": "monitor-record.md",
                    "content": "Append one entry per scheduled run\n",
                    "description": "Durable record of monitoring runs.",
                }
            ],
        }

        self.assertIsNotNone(
            update_session_skills(
                self.runtime_root,
                username=self.username,
                session_id=session_id,
                session_skills=[skill],
            )
        )
        record_path = session_skill_file_path(
            self.runtime_root,
            username=self.username,
            session_id=session_id,
            relative_path="monitor-record.md",
        )
        record_path.write_text("Append one entry per scheduled run\n\n- durable finding\n", encoding="utf-8")

        self.assertIsNotNone(
            update_session_skills(
                self.runtime_root,
                username=self.username,
                session_id=session_id,
                session_skills=[skill],
            )
        )

        self.assertEqual(
            record_path.read_text(encoding="utf-8"),
            "Append one entry per scheduled run\n\n- durable finding\n",
        )
        self.assertTrue(
            load_session_skills(
                self.runtime_root,
                username=self.username,
                session_id=session_id,
            )[0]["files"][0]["preserve_existing"]
        )

    def test_child_session_creation_preserves_session_skills(self) -> None:
        parent = create_conversation_session(self.runtime_root, username=self.username, label="Parent")
        child = create_child_conversation_session(
            self.runtime_root,
            username=self.username,
            parent_session_id=str(parent["session_id"]),
            label="Child",
            session_skills=[
                {
                    "skill_id": "dev-session",
                    "skill_scope": "template",
                    "canonical_session_key": "aize.development",
                }
            ],
        )

        self.assertIsNotNone(child)
        assert child is not None
        skills = load_session_skills(
            self.runtime_root,
            username=self.username,
            session_id=str(child["session_id"]),
        )
        self.assertEqual(skills[0]["skill_id"], "dev-session")
        self.assertEqual(skills[0]["skill_scope"], "unit")
        self.assertEqual(skills[0]["canonical_session_key"], "aize.development")

    def test_session_skill_runtime_scope_uses_unit_alias(self) -> None:
        self.assertEqual(_skill_scope({"skill_scope": "unit"}), "unit")
        self.assertEqual(_skill_scope({"skill_scope": "template"}), "unit")

    def test_child_session_sharing_policy_exposes_unit_alias(self) -> None:
        policy = normalize_child_session_sharing_policy(
            {"mode": "allowlist", "allowed_source_template_ids": ["aize-development.bug-hunting"]}
        )

        self.assertEqual(policy["allowed_source_unit_ids"], ["aize-development.bug-hunting"])
        self.assertEqual(policy["allowed_source_template_ids"], ["aize-development.bug-hunting"])

    def test_interactive_session_skill_runs_and_uses_ui_history_path(self) -> None:
        session = create_conversation_session(
            self.runtime_root,
            username=self.username,
            label="Interactive",
            communication_agent_enabled=True,
        )
        session_id = str(session["session_id"])
        update_session_skills(
            self.runtime_root,
            username=self.username,
            session_id=session_id,
            session_skills=[
                {
                    "skill_id": "echo-skill",
                    "skill_scope": "adaptive",
                    "kind": "interactive",
                    "routing_mode": "handle_user_message",
                    "routing_tags": ["echo"],
                    "handler_file": "skill.py",
                    "files": [
                        {
                            "path": "skill.py",
                            "content": (
                                "def handle(context):\n"
                                "    return {'assistant_text': 'skill saw: ' + context['prompt_text']}\n"
                            ),
                        }
                    ],
                }
            ],
        )
        refreshed = load_session_skills(
            self.runtime_root,
            username=self.username,
            session_id=session_id,
        )

        matches = matching_interactive_session_skills(
            {"session_skills": refreshed},
            prompt_text="please echo this",
        )
        self.assertEqual([item["skill_id"] for item in matches], ["echo-skill"])
        result = run_interactive_session_skill(
            self.runtime_root,
            username=self.username,
            session_id=session_id,
            skill=matches[0],
            prompt_text="please echo this",
            session=session,
        )
        self.assertEqual(result["assistant_text"], "skill saw: please echo this")

        append_history(
            self.runtime_root,
            username=self.username,
            session_id=session_id,
            entry={"direction": "out", "text": "please echo this"},
            limit=100,
        )
        append_session_skill_agent_turn(
            lambda user, sid, entry: append_history(
                self.runtime_root,
                username=user,
                session_id=sid,
                entry=entry,
                limit=100,
            ),
            username=self.username,
            session_id=session_id,
            skill=matches[0],
            text=result["assistant_text"],
        )
        ui_history = build_session_ui_history(
            self.runtime_root,
            username=self.username,
            session_id=session_id,
            limit=20,
        )
        raw_history = get_history(
            self.runtime_root,
            username=self.username,
            session_id=session_id,
        )
        self.assertTrue(any(entry.get("direction") == "in" and entry.get("text") == result["assistant_text"] for entry in ui_history))
        self.assertTrue(any(entry.get("event_type") == "turn.completed" for entry in raw_history))
        self.assertTrue(
            any(
                entry.get("skill_scope") == "adaptive"
                for entry in raw_history
                if entry.get("provider") == "session_skill"
            )
        )

    def test_interactive_session_skill_can_decline_prompt(self) -> None:
        session = create_conversation_session(
            self.runtime_root,
            username=self.username,
            label="Interactive",
            communication_agent_enabled=True,
        )
        session_id = str(session["session_id"])
        update_session_skills(
            self.runtime_root,
            username=self.username,
            session_id=session_id,
            session_skills=[
                {
                    "skill_id": "decline-skill",
                    "kind": "interactive",
                    "routing_mode": "handle_user_message",
                    "handler_file": "skill.py",
                    "files": [
                        {
                            "path": "skill.py",
                            "content": "def handle(context):\n    return {'handled': False}\n",
                        }
                    ],
                }
            ],
        )
        skill = load_session_skills(
            self.runtime_root,
            username=self.username,
            session_id=session_id,
        )[0]

        result = run_interactive_session_skill(
            self.runtime_root,
            username=self.username,
            session_id=session_id,
            skill=skill,
            prompt_text="route this to worker",
            session=session,
        )

        self.assertFalse(result["handled"])
        self.assertEqual(result["assistant_text"], "")


if __name__ == "__main__":
    unittest.main()
