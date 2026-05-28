from __future__ import annotations

import tempfile
import unittest
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from runtime.cli_service_adapter import provider_has_recent_fatal_error
from runtime.session_lifecycle import _provider_has_recent_fatal_error


class ProviderFatalLogTests(unittest.TestCase):
    def test_provider_fatal_error_reads_worker_failure_logs(self) -> None:
        with tempfile.TemporaryDirectory() as runtime_dir:
            runtime_root = Path(runtime_dir)
            logs_dir = runtime_root / ".aize-runtime" / "logs"
            logs_dir.mkdir(parents=True)
            (logs_dir / "service-gemini-001.jsonl").write_text(
                '{"type":"service.worker_failed","error":"FileNotFoundError(2, \\"No such file or directory\\")"}\n',
                encoding="utf-8",
            )

            self.assertTrue(provider_has_recent_fatal_error(runtime_root, provider="gemini"))
            self.assertTrue(_provider_has_recent_fatal_error(runtime_root, provider="gemini"))
            self.assertFalse(provider_has_recent_fatal_error(runtime_root, provider="codex"))
            self.assertFalse(_provider_has_recent_fatal_error(runtime_root, provider="codex"))

    def test_stale_service_state_does_not_block_provider(self) -> None:
        with tempfile.TemporaryDirectory() as runtime_dir:
            runtime_root = Path(runtime_dir)
            state_dir = runtime_root / ".aize-state" / "sessions" / "repyt" / "old" / "services"
            state_dir.mkdir(parents=True)
            stale = (datetime.now(UTC) - timedelta(days=3)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            (state_dir / "service-codex-001.json").write_text(
                json.dumps(
                    {
                        "service_id": "service-codex-001",
                        "updated_at": stale,
                        "error": "Authentication failed",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            self.assertFalse(provider_has_recent_fatal_error(runtime_root, provider="codex"))
            self.assertFalse(_provider_has_recent_fatal_error(runtime_root, provider="codex"))

    def test_summary_text_does_not_create_provider_wide_block(self) -> None:
        with tempfile.TemporaryDirectory() as runtime_dir:
            runtime_root = Path(runtime_dir)
            state_dir = runtime_root / ".aize-state" / "sessions" / "repyt" / "current" / "services"
            state_dir.mkdir(parents=True)
            now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            (state_dir / "service-codex-001.json").write_text(
                json.dumps(
                    {
                        "service_id": "service-codex-001",
                        "updated_at": now,
                        "status": "complete",
                        "goal_manager": {
                            "state": "idle",
                            "summary": "Reviewed a Gemini FileNotFoundError in a child session.",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            self.assertFalse(provider_has_recent_fatal_error(runtime_root, provider="codex"))
            self.assertFalse(_provider_has_recent_fatal_error(runtime_root, provider="codex"))


if __name__ == "__main__":
    unittest.main()
