from __future__ import annotations

from kernel.defs import (
    SESSION_RECIPIENT,
    USER_CONSOLE_RECIPIENT,
    normalize_endpoint as normalize_system_endpoint,
)


DEFAULT_AGENT_PROVIDER = "codex"
AGENT_EXECUTION_TIMEOUT_SECONDS = 600
DISPATCH_LEASE_RECOVERY_SECONDS = AGENT_EXECUTION_TIMEOUT_SECONDS + 60
GOAL_MANAGER_ROLE = "GoalManager"
WORKER_AGENT_ROLE = "WorkerAgent"
AGENT_ROLES = {GOAL_MANAGER_ROLE, WORKER_AGENT_ROLE}
ROLE_MESSAGE_RECIPIENTS = {
    GOAL_MANAGER_ROLE: {SESSION_RECIPIENT, USER_CONSOLE_RECIPIENT},
    WORKER_AGENT_ROLE: {SESSION_RECIPIENT},
}
DEFAULT_SESSION_CAPABILITIES = {
    "Session": [
        "accept_user_input_message",
        "store_session_messages",
        "provide_messages_to_agents_on_dispatch",
    ],
    GOAL_MANAGER_ROLE: [
        "read_session_goal",
        "read_session_goal_dag",
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
        "read_session_goal_dag",
        "read_session_messages",
        "work_toward_session_goal",
        "send_session_message",
        "attach_message_files",
    ],
}


def agent_endpoint(role: str) -> str:
    return f"agent:{role}"


def normalize_endpoint(value: str, *, session_id: str | None = None) -> str:
    raw = str(value or "").strip()
    if raw in AGENT_ROLES:
        return agent_endpoint(raw)
    return normalize_system_endpoint(raw, session_id=session_id)
