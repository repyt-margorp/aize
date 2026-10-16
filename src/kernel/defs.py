from __future__ import annotations

from typing import Any


STATE_VERSION = 1
ROOT_UNIT_ID = "root"
ROOT_SESSION_ID = "root"
ROOT_USERNAME = "root"
DEFAULT_ROOT_PASSWORD = "root"
SESSION_RECIPIENT = "Session"
USER_CONSOLE_RECIPIENT = "UserConsole"


class StoreError(RuntimeError):
    pass


def safe_id_part(value: str, *, fallback: str) -> str:
    safe_value = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in value)
    return safe_value.strip("-_") or fallback


def account_home_session_id(username: str) -> str:
    normalized = str(username or "").strip()
    return f"account-{safe_id_part(normalized, fallback='user')}"


def account_home_unit_id(username: str) -> str:
    return account_home_session_id(username)


def session_endpoint(session_id: str) -> str:
    return f"session:{session_id}"


def account_endpoint(username: str) -> str:
    return f"account:{username}"


def console_endpoint(endpoint_id: str) -> str:
    return f"console:{endpoint_id}"


def node_endpoint(node_id: str) -> str:
    return f"node:{node_id}"


def normalize_endpoint(value: str, *, session_id: str | None = None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "system"
    if ":" in raw or raw in {"system", "dispatcher"}:
        return raw
    if raw == SESSION_RECIPIENT:
        if not session_id:
            raise StoreError("Session endpoint requires session_id")
        return session_endpoint(session_id)
    if raw == USER_CONSOLE_RECIPIENT:
        return USER_CONSOLE_RECIPIENT
    if raw == "remote-aize":
        return node_endpoint("remote-aize")
    return account_endpoint(raw)


def payload_body(message: dict[str, Any]) -> str:
    payload = message.get("payload")
    if isinstance(payload, dict):
        for key in ("body", "text"):
            value = payload.get(key)
            if value is not None:
                return str(value)
    return str(message.get("body") or "")


def payload_files(message: dict[str, Any]) -> list[dict[str, Any]]:
    payload = message.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("files"), list):
        return [item for item in payload["files"] if isinstance(item, dict)]
    return []
