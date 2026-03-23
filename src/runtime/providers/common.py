from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def schema_path(schema_id: str) -> Path:
    return Path(__file__).resolve().parents[1] / "schemas" / f"{schema_id}.json"


def schema_text(schema_id: str) -> str:
    return schema_path(schema_id).read_text(encoding="utf-8")


def parse_stream_json_line(line: str) -> dict[str, Any]:
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return {"type": "stdout.raw", "line": line}


def normalize_stream_event(provider: str, record: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(record)
    normalized["provider"] = provider
    if not normalized.get("type"):
        normalized["type"] = f"{provider}.event"
    return normalized
