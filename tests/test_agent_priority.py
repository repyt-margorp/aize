from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from runtime.persistent_state_pkg._core import (
    active_agent_priority,
    active_goal_manager_priority,
    normalize_agent_priority,
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


if __name__ == "__main__":
    unittest.main()
