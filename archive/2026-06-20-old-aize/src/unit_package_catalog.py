from __future__ import annotations

import json
import os
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def configured_unit_package_roots() -> list[Path]:
    raw = os.environ.get("AIZE_UNIT_PACKAGE_ROOTS", "").strip()
    if raw:
        roots = [Path(part).expanduser() for part in raw.split(os.pathsep) if part.strip()]
    else:
        roots = [repo_root() / "unit_packages"]
    seen: set[Path] = set()
    ordered: list[Path] = []
    for root in roots:
        resolved = root.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        ordered.append(resolved)
    return ordered


def _is_hidden(path: Path) -> bool:
    return any(part.startswith(".") for part in path.parts)


def list_unit_package_dirs() -> list[Path]:
    package_dirs: list[Path] = []
    for root in configured_unit_package_roots():
        if not root.exists():
            continue
        for manifest_path in sorted(root.rglob("unit-package.json")):
            if not manifest_path.is_file():
                continue
            if _is_hidden(manifest_path.relative_to(root)):
                continue
            package_dirs.append(manifest_path.parent)
    return package_dirs


def load_unit_package_manifest(package_dir: Path) -> dict:
    manifest_path = package_dir / "unit-package.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data.setdefault("package_id", package_dir.name)
    data["_unit_package_dir"] = str(package_dir)
    return data


def list_unit_package_manifests() -> list[dict]:
    manifests: list[dict] = []
    for package_dir in list_unit_package_dirs():
        try:
            manifests.append(load_unit_package_manifest(package_dir))
        except FileNotFoundError:
            continue
    return manifests


def _module_name_for_path(path: Path) -> str:
    relative = path.resolve().relative_to(repo_root())
    return ".".join(relative.parts)


def _descriptor_with_defaults(descriptor_path: Path, *, descriptor_type: str) -> dict:
    data = json.loads(descriptor_path.read_text(encoding="utf-8"))
    data["_descriptor_path"] = str(descriptor_path)
    data["_descriptor_dir"] = str(descriptor_path.parent)
    if descriptor_type == "unit":
        unit_id = str(data.get("unit_id") or "").strip()
        if unit_id:
            data.setdefault("unit_id", unit_id)
    if descriptor_type == "service":
        data.setdefault("module", _module_name_for_path(descriptor_path.parent))
    return data


def list_unit_package_service_descriptors() -> list[dict]:
    descriptors: list[dict] = []
    for package_dir in list_unit_package_dirs():
        try:
            package_manifest = load_unit_package_manifest(package_dir)
        except FileNotFoundError:
            continue
        services_dir = package_dir / "services"
        if not services_dir.exists():
            continue
        for descriptor_path in sorted(services_dir.glob("*/service.json")):
            descriptor = _descriptor_with_defaults(descriptor_path, descriptor_type="service")
            descriptor.setdefault("package_id", package_manifest["package_id"])
            descriptors.append(descriptor)
    return descriptors


def list_unit_file_descriptors() -> list[dict]:
    descriptors: list[dict] = []
    for package_dir in list_unit_package_dirs():
        try:
            package_manifest = load_unit_package_manifest(package_dir)
        except FileNotFoundError:
            continue
        descriptors_dir = package_dir / "units"
        if not descriptors_dir.exists():
            continue
        for descriptor_path in sorted(descriptors_dir.glob("*/unit.json")):
            descriptor = _descriptor_with_defaults(descriptor_path, descriptor_type="unit")
            descriptor.setdefault("package_id", package_manifest["package_id"])
            descriptor.setdefault(
                "catalog_visibility",
                str(package_manifest.get("catalog_visibility") or "").strip().lower() or "public",
            )
            descriptors.append(descriptor)
    return descriptors
