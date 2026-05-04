from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from runtime.persistent_state_pkg._core import (
    DEFAULT_INTERACTIVE_AGENT_PROFILE_PRIORITY,
    active_agent_priority,
    active_goal_manager_priority,
    active_agent_profile_priority,
    normalize_agent_priority,
    normalize_agent_profile_priority,
    normalize_goal_manager_priority,
)


class AgentPriorityNormalizationTests(unittest.TestCase):
    def test_existing_sessions_gain_missing_gemini_below_border(self) -> None:
        self.assertEqual(
            normalize_agent_priority(["codex", "claude", "border"]),
            ["codex", "claude", "border", "gemini"],
        )

    def test_unknown_priority_token_is_preserved_as_provider_kind(self) -> None:
        self.assertEqual(
            normalize_agent_priority(["claude", "external-typo"]),
            ["claude", "external-typo", "border", "codex", "gemini"],
        )

    def test_agent_priority_can_activate_external_provider_when_available(self) -> None:
        self.assertEqual(
            active_agent_priority(["ws-peer", "codex", "border"], available_kinds={"codex", "ws-peer"}),
            ["ws-peer", "codex"],
        )

    def test_goal_manager_priority_defaults_to_native_provider_set(self) -> None:
        self.assertEqual(normalize_goal_manager_priority(None), ["codex", "claude", "gemini", "border"])
        self.assertEqual(
            active_goal_manager_priority(["ws-peer", "codex", "border"], available_kinds={"codex", "ws-peer"}),
            ["ws-peer", "codex"],
        )

    def test_agent_profile_priority_preserves_provider_settings(self) -> None:
        priority = normalize_agent_profile_priority(
            [
                {
                    "provider": "codex",
                    "profile": "interactive-fast",
                    "model": "gpt-5.4-mini",
                    "session_slot": "interactive_agent",
                    "session_mode": "ephemeral",
                    "ephemeral": True,
                    "reasoning_effort": "low",
                    "verbosity": "low",
                },
                "border",
                "claude",
            ]
        )
        self.assertEqual(
            priority[0],
            {
                "provider": "codex",
                "profile": "interactive-fast",
                "model": "gpt-5.4-mini",
                "session_slot": "interactive_agent",
                "session_mode": "ephemeral",
                "ephemeral": True,
                "config": {
                    "model_reasoning_effort": "low",
                    "model_verbosity": "low",
                },
            },
        )
        self.assertEqual(active_agent_profile_priority(priority)[0]["provider"], "codex")

    def test_interactive_profile_default_uses_codex_default_reasoning(self) -> None:
        priority = normalize_agent_profile_priority(DEFAULT_INTERACTIVE_AGENT_PROFILE_PRIORITY)
        self.assertEqual(priority[0]["provider"], "codex")
        self.assertEqual(priority[0]["model"], "gpt-5.4-mini")
        self.assertEqual(priority[0]["session_slot"], "interactive_agent")
        self.assertEqual(priority[0]["session_mode"], "ephemeral")
        self.assertTrue(priority[0]["ephemeral"])
        self.assertNotIn("config", priority[0])


if __name__ == "__main__":
    unittest.main()
