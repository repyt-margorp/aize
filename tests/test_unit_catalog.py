from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from runtime.http_handler import _unit_launched_sessions_for_user
from runtime.persistent_state_pkg import (
    create_conversation_session,
    update_session_launcher_profile,
)


class UnitCatalogTests(unittest.TestCase):
    def test_authenticated_units_catalog_includes_private_units(self) -> None:
        source = (ROOT / "src" / "runtime" / "http_handler.py").read_text(encoding="utf-8")

        self.assertIn(
            "list_launchable_units(default_provider=default_provider, include_private=include_private)",
            source,
        )
        self.assertIn('query.get("include_private")', source)
        self.assertIn('or ["1"]', source)

    def test_unit_catalog_ui_has_visibility_and_schedule_controls(self) -> None:
        source = (ROOT / "src" / "runtime" / "html_renderer.py").read_text(encoding="utf-8")

        self.assertIn("unit-catalog-include-private", source)
        self.assertIn("unit-catalog-public-only", source)
        self.assertIn("include_private=${visibilityParam}", source)
        self.assertIn("unit-launcher-schedule-editor", source)
        self.assertIn("fetch('/units/schedule'", source)
        self.assertIn("unitScheduleEditingUnitId", source)

    def test_unit_catalog_groups_launched_sessions_by_unit(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            runtime_root = Path(tempdir) / ".aize-runtime"
            runtime_root.mkdir(parents=True)
            first = create_conversation_session(runtime_root, username="repyt", label="Entrance Alpha")
            second = create_conversation_session(runtime_root, username="repyt", label="Entrance Beta")
            other = create_conversation_session(runtime_root, username="repyt", label="Diagnostics")

            update_session_launcher_profile(
                runtime_root,
                username="repyt",
                session_id=str(first["session_id"]),
                launcher_unit_id="entrance.service",
                launcher_display_name="Entrance",
                preferred_provider="codex",
                selected_agents=[],
                service_targets=[],
                launcher_unit_kind="interface",
            )
            update_session_launcher_profile(
                runtime_root,
                username="repyt",
                session_id=str(second["session_id"]),
                launcher_unit_id="entrance.service",
                launcher_display_name="Entrance",
                preferred_provider="codex",
                selected_agents=[],
                service_targets=[],
                launcher_unit_kind="interface",
            )
            update_session_launcher_profile(
                runtime_root,
                username="repyt",
                session_id=str(other["session_id"]),
                launcher_unit_id="diagnostics.service",
                launcher_display_name="AIze System Diagnostics",
                preferred_provider="codex",
                selected_agents=[],
                service_targets=[],
                launcher_unit_kind="session",
            )

            grouped = _unit_launched_sessions_for_user(runtime_root, viewer_username="repyt")

        self.assertEqual(
            {item["label"] for item in grouped["entrance.service"]},
            {"Entrance Alpha", "Entrance Beta"},
        )
        self.assertEqual(grouped["diagnostics.service"][0]["label"], "Diagnostics")
        self.assertEqual(grouped["entrance.service"][0]["session_ui_mode"], "standard")


if __name__ == "__main__":
    unittest.main()
