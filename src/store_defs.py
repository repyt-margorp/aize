from __future__ import annotations

from typing import Any


STATE_VERSION = 1
ROOT_UNIT_ID = "root"
ROOT_SESSION_ID = "root"
ROOT_USERNAME = "root"
DEFAULT_ROOT_PASSWORD = "root"
PASSWORD_HASH_ITERATIONS = 210_000
GOAL_MANAGER_ROLE = "GoalManager"
WORKER_AGENT_ROLE = "WorkerAgent"
SESSION_RECIPIENT = "Session"
USER_CONSOLE_RECIPIENT = "UserConsole"
AGENT_ROLES = {GOAL_MANAGER_ROLE, WORKER_AGENT_ROLE}
ROLE_MESSAGE_RECIPIENTS = {
    GOAL_MANAGER_ROLE: {SESSION_RECIPIENT, USER_CONSOLE_RECIPIENT},
    WORKER_AGENT_ROLE: {SESSION_RECIPIENT},
}
DEFAULT_AGENT_PROVIDER = "codex"
DISPATCH_PRIORITY_USER_INPUT = 100
DISPATCH_PRIORITY_WORKER_REQUEST = 150
DISPATCH_PRIORITY_GOAL = 50
DISPATCH_PRIORITY_RETRY = 25
DEFAULT_SESSION_CAPABILITIES = {
    "Session": [
        "accept_user_input_message",
        "store_session_messages",
        "provide_messages_to_agents_on_dispatch",
    ],
    GOAL_MANAGER_ROLE: [
        "read_session_goal",
        "read_session_messages",
        "evaluate_session_goal_completion",
        "write_goal_completion_state",
        "write_goal_completion_reason_message",
        "send_session_message",
        "send_user_console_message_to_reply_endpoint",
        "send_worker_request",
    ],
    WORKER_AGENT_ROLE: [
        "read_session_goal",
        "read_session_messages",
        "work_toward_session_goal",
        "send_session_message",
        "attach_message_files",
    ],
}


class StoreError(RuntimeError):
    pass


def session_endpoint(session_id: str) -> str:
    return f"session:{session_id}"


def account_endpoint(username: str) -> str:
    return f"account:{username}"


def agent_endpoint(role: str) -> str:
    return f"agent:{role}"


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
    if raw in AGENT_ROLES:
        return agent_endpoint(raw)
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
