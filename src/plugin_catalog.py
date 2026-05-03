from __future__ import annotations

import json
import os
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def configured_plugin_roots() -> list[Path]:
    raw = os.environ.get("AIZE_PLUGIN_ROOTS", "").strip()
    if raw:
        roots = [Path(part).expanduser() for part in raw.split(os.pathsep) if part.strip()]
    else:
        roots = [repo_root() / "plugins"]
    seen: set[Path] = set()
    ordered: list[Path] = []
    for root in roots:
        resolved = root.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        ordered.append(resolved)
    return ordered


def configured_unit_package_roots() -> list[Path]:
    return configured_plugin_roots()


def _is_hidden(path: Path) -> bool:
    return any(part.startswith(".") for part in path.parts)


def list_plugin_dirs() -> list[Path]:
    plugin_dirs: list[Path] = []
    for root in configured_plugin_roots():
        if not root.exists():
            continue
        for manifest_path in sorted(root.rglob("plugin.json")):
            if _is_hidden(manifest_path.relative_to(root)):
                continue
            plugin_dirs.append(manifest_path.parent)
    return plugin_dirs


def list_unit_package_dirs() -> list[Path]:
    return list_plugin_dirs()


def load_plugin_manifest(plugin_dir: Path) -> dict:
    manifest_path = plugin_dir / "plugin.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data.setdefault("plugin_id", plugin_dir.name)
    data.setdefault("package_id", data.get("plugin_id") or plugin_dir.name)
    data["_plugin_dir"] = str(plugin_dir)
    return data


def load_unit_package_manifest(package_dir: Path) -> dict:
    return load_plugin_manifest(package_dir)


def list_plugin_manifests() -> list[dict]:
    return [load_plugin_manifest(plugin_dir) for plugin_dir in list_plugin_dirs()]


def list_unit_package_manifests() -> list[dict]:
    return list_plugin_manifests()


def _module_name_for_path(path: Path) -> str:
    relative = path.resolve().relative_to(repo_root())
    return ".".join(relative.parts)


def _descriptor_with_defaults(descriptor_path: Path, *, descriptor_type: str) -> dict:
    data = json.loads(descriptor_path.read_text(encoding="utf-8"))
    data["_descriptor_path"] = str(descriptor_path)
    data["_descriptor_dir"] = str(descriptor_path.parent)
    if descriptor_type == "service":
        data.setdefault("module", _module_name_for_path(descriptor_path.parent))
    return data


def list_plugin_service_descriptors() -> list[dict]:
    descriptors: list[dict] = []
    for plugin_dir in list_plugin_dirs():
        plugin_manifest = load_plugin_manifest(plugin_dir)
        services_dir = plugin_dir / "services"
        if not services_dir.exists():
            continue
        for descriptor_path in sorted(services_dir.glob("*/service.json")):
            descriptor = _descriptor_with_defaults(descriptor_path, descriptor_type="service")
            descriptor.setdefault("plugin_id", plugin_manifest["plugin_id"])
            descriptors.append(descriptor)
    return descriptors


def list_plugin_session_template_descriptors() -> list[dict]:
    descriptors: list[dict] = []
    for plugin_dir in list_plugin_dirs():
        plugin_manifest = load_plugin_manifest(plugin_dir)
        descriptor_roots = [
            plugin_dir / "units",
            plugin_dir / "session-templates",
            plugin_dir / "apps",
        ]
        for descriptors_dir in descriptor_roots:
            if not descriptors_dir.exists():
                continue
            for pattern in ("*/unit.json", "*/session-template.json", "*/app.json"):
                for descriptor_path in sorted(descriptors_dir.glob(pattern)):
                    descriptor = _descriptor_with_defaults(descriptor_path, descriptor_type="app")
                    descriptor.setdefault("plugin_id", plugin_manifest["plugin_id"])
                    descriptor.setdefault("package_id", plugin_manifest.get("package_id") or plugin_manifest["plugin_id"])
                    descriptors.append(descriptor)
    return descriptors


def list_unit_file_descriptors() -> list[dict]:
    return list_plugin_session_template_descriptors()


def list_plugin_app_descriptors() -> list[dict]:
    # Backward-compatible alias for older callers.
    return list_plugin_session_template_descriptors()
