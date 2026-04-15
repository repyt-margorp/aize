from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from runtime.persistent_state_pkg._core import normalize_agent_priority


class AgentPriorityNormalizationTests(unittest.TestCase):
    def test_existing_sessions_gain_missing_gemini_below_border(self) -> None:
        self.assertEqual(
            normalize_agent_priority(["codex", "claude", "border"]),
            ["codex", "claude", "border", "gemini"],
        )

    def test_boarder_alias_preserves_divider_and_appends_missing_provider(self) -> None:
        self.assertEqual(
            normalize_agent_priority(["claude", "boarder"]),
            ["claude", "border", "codex", "gemini"],
        )


if __name__ == "__main__":
    unittest.main()
