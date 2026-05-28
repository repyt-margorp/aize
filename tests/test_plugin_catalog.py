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

from plugin_catalog import (
    list_plugin_app_descriptors,
    list_plugin_dirs,
    list_plugin_manifests,
    list_plugin_service_descriptors,
    list_plugin_session_template_descriptors,
)
from unit_catalog import (
    list_unit_file_descriptors,
    list_unit_package_manifests,
    list_unit_package_service_descriptors,
)
from services import load_service_handler
from services.svcmgr.loader import get_service_descriptor, list_service_descriptors


class PluginCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.mkdtemp(prefix="test_plugin_", dir=ROOT / "plugins")
        self.plugin_dir = Path(self.tempdir)
        (self.plugin_dir / "plugin.json").write_text(
            json.dumps(
                {
                    "plugin_id": self.plugin_dir.name,
                    "display_name": "Test Plugin",
                    "catalog_visibility": "private",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        service_dir = self.plugin_dir / "services" / "secret_worker"
        service_dir.mkdir(parents=True, exist_ok=True)
        (service_dir / "__init__.py").write_text(
            "def run_service(**_kwargs):\n    return 0\n",
            encoding="utf-8",
        )
        (service_dir / "service.json").write_text(
            json.dumps(
                {
                    "kind": "secret_worker",
                    "id_prefix": "service-secret-worker",
                    "pool_size_default": 1,
                    "display_name_template": "Secret Worker {index}",
                    "persona": "Private worker",
                    "max_turns": 10,
                    "enabled": True,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        unit_dir = self.plugin_dir / "units" / "launcher"
        unit_dir.mkdir(parents=True, exist_ok=True)
        (unit_dir / "unit.json").write_text(
            json.dumps({"unit_id": "launcher", "display_name": "Launcher", "launcher": {}}) + "\n",
            encoding="utf-8",
        )
        legacy_dir = self.plugin_dir / "apps" / "legacy_launcher"
        legacy_dir.mkdir(parents=True, exist_ok=True)
        (legacy_dir / "app.json").write_text(
            json.dumps({"template_id": "legacy_launcher", "display_name": "Legacy Launcher", "launcher": {}}) + "\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.plugin_dir, ignore_errors=True)

    def test_plugin_descriptors_are_discovered(self) -> None:
        with patch.dict("os.environ", {"AIZE_PLUGIN_ROOTS": str(ROOT / "plugins")}):
            manifests = list_plugin_manifests()
            services = list_plugin_service_descriptors()
            templates = list_plugin_session_template_descriptors()
            apps = list_plugin_app_descriptors()
            unit_manifests = list_unit_package_manifests()
            unit_services = list_unit_package_service_descriptors()
            unit_files = list_unit_file_descriptors()

        self.assertTrue(any(item["plugin_id"] == self.plugin_dir.name for item in manifests))
        self.assertTrue(any(item["package_id"] == self.plugin_dir.name for item in unit_manifests))
        service = next(item for item in services if item["kind"] == "secret_worker")
        self.assertEqual(service["plugin_id"], self.plugin_dir.name)
        self.assertEqual(service["module"], f"plugins.{self.plugin_dir.name}.services.secret_worker")
        self.assertTrue(any(item["kind"] == "secret_worker" for item in unit_services))
        template = next(item for item in templates if item["unit_id"] == "launcher")
        self.assertEqual(template["plugin_id"], self.plugin_dir.name)
        self.assertEqual(template["catalog_visibility"], "private")
        self.assertTrue(any(item.get("unit_id") == "launcher" for item in unit_files))
        app = next(
            item
            for item in apps
            if str(item.get("unit_id") or item.get("template_id") or "") == "launcher"
        )
        self.assertEqual(app["plugin_id"], self.plugin_dir.name)
        self.assertEqual(app["catalog_visibility"], "private")

    def test_legacy_plugin_app_descriptor_alias_remains_compatible(self) -> None:
        with patch.dict("os.environ", {"AIZE_PLUGIN_ROOTS": str(ROOT / "plugins")}):
            descriptors = list_plugin_app_descriptors()

        self.assertTrue(
            any(str(item.get("unit_id") or item.get("template_id") or "") == "legacy_launcher" for item in descriptors)
        )

    def test_service_loader_uses_plugin_module(self) -> None:
        with patch.dict("os.environ", {"AIZE_PLUGIN_ROOTS": str(ROOT / "plugins")}):
            descriptor = get_service_descriptor("secret_worker")
            handler = load_service_handler("secret_worker")
            visible_kinds = {item["kind"] for item in list_service_descriptors(exclude_kinds=set())}

        self.assertEqual(descriptor["module"], f"plugins.{self.plugin_dir.name}.services.secret_worker")
        self.assertIn("secret_worker", visible_kinds)
        self.assertEqual(handler(), 0)

    def test_catalog_skips_plugin_removed_after_discovery(self) -> None:
        with patch.dict("os.environ", {"AIZE_PLUGIN_ROOTS": str(ROOT / "plugins")}):
            package_dirs = list_plugin_dirs()
            self.assertIn(self.plugin_dir, package_dirs)
            shutil.rmtree(self.plugin_dir)
            manifests = list_plugin_manifests()
            templates = list_plugin_session_template_descriptors()
            services = list_plugin_service_descriptors()

        self.assertFalse(any(item.get("plugin_id") == self.plugin_dir.name for item in manifests))
        self.assertFalse(any(item.get("plugin_id") == self.plugin_dir.name for item in templates))
        self.assertFalse(any(item.get("plugin_id") == self.plugin_dir.name for item in services))


if __name__ == "__main__":
    unittest.main()
