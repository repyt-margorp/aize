from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any


def utc_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def make_message(
    *,
    from_node_id: str,
    from_service_id: str,
    to_node_id: str,
    to_service_id: str,
    message_type: str,
    run_id: str,
    payload: dict[str, Any] | None = None,
    payload_ref: str | None = None,
) -> dict[str, Any]:
    meta = {
        "message_id": f"msg-{uuid.uuid4()}",
        "run_id": run_id,
        "from_node": from_node_id,
        "to_node": to_node_id,
        "ts": utc_ts(),
    }
    message = {
        "from": from_service_id,
        "to": to_service_id,
        "type": message_type,
        "meta": meta,
    }
    if payload is not None:
        message["payload"] = payload
    if payload_ref is not None:
        message["payload_ref"] = payload_ref
    return message


def message_meta(message: dict[str, Any]) -> dict[str, Any]:
    meta = message.get("meta")
    if isinstance(meta, dict):
        return meta
    return {}


def message_meta_get(message: dict[str, Any], key: str, default: Any = None) -> Any:
    meta = message_meta(message)
    if key in meta:
        return meta[key]
    return message.get(key, default)


def message_set_meta(message: dict[str, Any], key: str, value: Any) -> None:
    meta = dict(message_meta(message))
    meta[key] = value
    message["meta"] = meta
    message[key] = value


def write_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def encode_line(record: dict[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False) + "\n"


def decode_line(line: str) -> dict[str, Any]:
    return json.loads(line)


def store_text_object(objects_dir: Path, text: str, *, kind: str = "text") -> str:
    object_id = f"obj-{uuid.uuid4()}"
    object_path = objects_dir / f"{object_id}.json"
    object_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "object_id": object_id,
        "kind": kind,
        "created_at": utc_ts(),
        "text": text,
    }
    object_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return object_id


def load_text_object(objects_dir: Path, object_id: str) -> str:
    object_path = objects_dir / f"{object_id}.json"
    record = json.loads(object_path.read_text(encoding="utf-8"))
    return str(record["text"])
