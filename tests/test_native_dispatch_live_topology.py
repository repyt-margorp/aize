from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class NativeDispatchLiveTopologyTests(unittest.TestCase):
    def test_adapter_refreshes_llm_topology_for_dispatch(self) -> None:
        source = (SRC / "runtime" / "cli_service_adapter.py").read_text(encoding="utf-8")
        self.assertIn("def current_llm_service_topology()", source)
        self.assertIn("current_codex_service_pool, current_claude_service_pool, current_gemini_service_pool", source)
        self.assertIn("all_local_service_ids = set(current_codex_service_pool) | set(current_claude_service_pool) | set(current_gemini_service_pool)", source)
        self.assertIn('"gemini": current_gemini_service_pool', source)
        self.assertIn("current_llm_service_topology=current_llm_service_topology", source)

    def test_http_handler_uses_live_topology_for_prompt_and_timeout_dispatch(self) -> None:
        source = (SRC / "runtime" / "http_handler.py").read_text(encoding="utf-8")
        self.assertIn("current_llm_service_topology()", source)
        self.assertIn("all_local_service_ids = (", source)
        self.assertIn('"gemini": current_gemini_service_pool', source)
        self.assertIn("current_codex_service_pool, current_claude_service_pool, current_gemini_service_pool", source)
        self.assertIn("current_gemini_service_pool if preferred_provider == \"gemini\" else current_codex_service_pool", source)


if __name__ == "__main__":
    unittest.main()
