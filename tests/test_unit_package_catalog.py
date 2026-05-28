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

from unit_package_catalog import (
    list_unit_package_dirs,
    list_unit_package_manifests,
    list_unit_package_service_descriptors,
)
from unit_catalog import (
    list_unit_file_descriptors,
    list_unit_package_manifests,
    list_unit_package_service_descriptors,
)
from services import load_service_handler
from services.svcmgr.loader import get_service_descriptor, list_service_descriptors


class UnitPackageCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.mkdtemp(prefix="test_unit_package_", dir=ROOT / "unit_packages")
        self.unit_package_dir = Path(self.tempdir)
        (self.unit_package_dir / "unit-package.json").write_text(
            json.dumps(
                {
                    "package_id": self.unit_package_dir.name,
                    "display_name": "Test Unit Package",
                    "catalog_visibility": "private",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        service_dir = self.unit_package_dir / "services" / "secret_worker"
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
        unit_dir = self.unit_package_dir / "units" / "launcher"
        unit_dir.mkdir(parents=True, exist_ok=True)
        (unit_dir / "unit.json").write_text(
            json.dumps({"unit_id": "launcher", "display_name": "Launcher", "launcher": {}}) + "\n",
            encoding="utf-8",
        )
    def tearDown(self) -> None:
        shutil.rmtree(self.unit_package_dir, ignore_errors=True)

    def test_unit_package_descriptors_are_discovered(self) -> None:
        with patch.dict("os.environ", {"AIZE_UNIT_PACKAGE_ROOTS": str(ROOT / "unit_packages")}):
            manifests = list_unit_package_manifests()
            services = list_unit_package_service_descriptors()
            unit_manifests = list_unit_package_manifests()
            unit_services = list_unit_package_service_descriptors()
            unit_files = list_unit_file_descriptors()

        self.assertTrue(any(item["package_id"] == self.unit_package_dir.name for item in manifests))
        self.assertTrue(any(item["package_id"] == self.unit_package_dir.name for item in unit_manifests))
        service = next(item for item in services if item["kind"] == "secret_worker")
        self.assertEqual(service["package_id"], self.unit_package_dir.name)
        self.assertEqual(service["module"], f"unit_packages.{self.unit_package_dir.name}.services.secret_worker")
        self.assertTrue(any(item["kind"] == "secret_worker" for item in unit_services))
        template = next(item for item in unit_files if item["unit_id"] == "launcher")
        self.assertEqual(template["package_id"], self.unit_package_dir.name)
        self.assertEqual(template["catalog_visibility"], "private")
        self.assertTrue(any(item.get("unit_id") == "launcher" for item in unit_files))

    def test_service_loader_uses_unit_package_module(self) -> None:
        with patch.dict("os.environ", {"AIZE_UNIT_PACKAGE_ROOTS": str(ROOT / "unit_packages")}):
            descriptor = get_service_descriptor("secret_worker")
            handler = load_service_handler("secret_worker")
            visible_kinds = {item["kind"] for item in list_service_descriptors(exclude_kinds=set())}

        self.assertEqual(descriptor["module"], f"unit_packages.{self.unit_package_dir.name}.services.secret_worker")
        self.assertIn("secret_worker", visible_kinds)
        self.assertEqual(handler(), 0)

    def test_catalog_skips_unit_package_removed_after_discovery(self) -> None:
        with patch.dict("os.environ", {"AIZE_UNIT_PACKAGE_ROOTS": str(ROOT / "unit_packages")}):
            package_dirs = list_unit_package_dirs()
            self.assertIn(self.unit_package_dir, package_dirs)
            shutil.rmtree(self.unit_package_dir)
            manifests = list_unit_package_manifests()
            services = list_unit_package_service_descriptors()

        self.assertFalse(any(item.get("package_id") == self.unit_package_dir.name for item in manifests))
        self.assertFalse(any(item.get("package_id") == self.unit_package_dir.name for item in services))


if __name__ == "__main__":
    unittest.main()
