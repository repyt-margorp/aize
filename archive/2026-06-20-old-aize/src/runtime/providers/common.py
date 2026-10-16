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
    method = str(record.get("method") or "").strip()
    params = record.get("params")
    if method and isinstance(params, dict):
        if method == "item/agentMessage/delta":
            return {
                "type": "agent_message.delta",
                "delta": str(params.get("delta") or ""),
                "provider": provider,
                "raw_method": method,
            }
        if method in {"item/agentMessage/completed", "item/agentMessage/complete"}:
            return {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": str(params.get("text") or params.get("message") or ""),
                },
                "provider": provider,
                "raw_method": method,
            }
    normalized = dict(record)
    normalized["provider"] = provider
    if not normalized.get("type"):
        normalized["type"] = f"{provider}.event"
    return normalized
