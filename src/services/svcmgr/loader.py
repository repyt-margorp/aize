"""
Service descriptor loader for the Service Manager.

Reads service.json files from src/services/*/service.json and expands
them into concrete service specs, resolving environment variables.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def _services_pkg_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def list_service_descriptors(*, exclude_kinds: set[str] | None = None) -> list[dict]:
    """Scan services/*/service.json and return enabled descriptors, sorted by kind name."""
    pkg_dir = _services_pkg_dir()
    exclude = exclude_kinds if exclude_kinds is not None else {"svcmgr"}
    descriptors = []
    for d in sorted(pkg_dir.iterdir()):
        if not d.is_dir() or d.name.startswith("_") or d.name in exclude:
            continue
        desc_file = d / "service.json"
        if not desc_file.exists():
            continue
        desc = json.loads(desc_file.read_text(encoding="utf-8"))
        if not desc.get("enabled", True):
            continue
        descriptors.append(desc)
    return descriptors


def _resolve_config_env(config_env: dict) -> dict:
    """Resolve config_env entries against environment variables."""
    config = {}
    for key, cfg in config_env.items():
        if isinstance(cfg, dict):
            env_val = os.environ.get(cfg["env"]) if "env" in cfg else None
            raw = env_val if env_val is not None else cfg.get("default")
            typ = cfg.get("type")
            if typ == "int" and raw is not None:
                raw = int(raw)
            elif typ == "bool" and isinstance(raw, str):
                raw = raw.strip().lower() not in {"false", "0", "no", "off"}
            config[key] = raw
        else:
            config[key] = cfg
    return config


def expand_descriptor(desc: dict) -> list[dict]:
    """Expand a service descriptor into one or more concrete service specs."""
    kind = desc["kind"]

    if "id" in desc:
        # Single fixed-ID service
        config = _resolve_config_env(desc.get("config_env", {}))
        config.update(desc.get("config", {}))
        spec: dict = {
            "service_id": desc["id"],
            "kind": kind,
            "display_name": desc.get("display_name", desc["id"]),
            "persona": desc.get("persona", ""),
            "max_turns": desc.get("max_turns", 100),
            "spawn_order": desc.get("spawn_order", 100),
        }
        if rsi := desc.get("response_schema_id"):
            spec["response_schema_id"] = rsi
        if config:
            spec["config"] = config
        return [spec]

    # Pool service: expand into multiple instances
    id_prefix = desc["id_prefix"]
    pool_env = desc.get("pool_size_env")
    pool_default = desc.get("pool_size_default", 1)
    pool_size = int(os.environ.get(pool_env, pool_default)) if pool_env else pool_default

    specs = []
    for i in range(1, pool_size + 1):
        service_id = f"{id_prefix}-{i:03d}"
        display_template = desc.get("display_name_template", f"{kind} {{index}}")
        display_name = display_template.replace("{index}", str(i))
        config = _resolve_config_env(desc.get("config_env", {}))
        config.update(desc.get("config", {}))
        spec = {
            "service_id": service_id,
            "kind": kind,
            "display_name": display_name,
            "persona": desc.get("persona", ""),
            "max_turns": desc.get("max_turns", 100),
            "spawn_order": desc.get("spawn_order", 100),
        }
        if rsi := desc.get("response_schema_id"):
            spec["response_schema_id"] = rsi
        if config:
            spec["config"] = config
        specs.append(spec)
    return specs


def build_service_plan(descriptors: list[dict]) -> list[dict]:
    """Expand all descriptors into a flat list of service specs."""
    specs = []
    for desc in descriptors:
        specs.extend(expand_descriptor(desc))
    return specs
