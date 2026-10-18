from __future__ import annotations

from kernel.defs import (
    SESSION_RECIPIENT,
    USER_CONSOLE_RECIPIENT,
    normalize_endpoint as normalize_system_endpoint,
    user_endpoint,
)


DEFAULT_AGENT_PROVIDER = "codex"
AGENT_EXECUTION_TIMEOUT_SECONDS = 600
DISPATCH_LEASE_RECOVERY_SECONDS = AGENT_EXECUTION_TIMEOUT_SECONDS + 60
GOAL_MANAGER_ROLE = "GoalManager"
WORKER_AGENT_ROLE = "WorkerAgent"
AGENT_ROLES = {GOAL_MANAGER_ROLE, WORKER_AGENT_ROLE}

# Slot vocabulary: the job classes have group names that service users join to
# become eligible for appointment. Role tokens stay as durable protocol names.
SLOT_GROUPS = {
    GOAL_MANAGER_ROLE: "manage",
    WORKER_AGENT_ROLE: "work",
}
SLOT_ROLE_BY_GROUP = {group: role for role, group in SLOT_GROUPS.items()}
ROLE_ALIASES = {
    "goalmanager": GOAL_MANAGER_ROLE,
    "workeragent": WORKER_AGENT_ROLE,
    "worker": WORKER_AGENT_ROLE,
    "manage": GOAL_MANAGER_ROLE,
    "work": WORKER_AGENT_ROLE,
}

# Provider entities auto-registered as non-login service users. Groups carry
# the appointment eligibility; remote providers are never manager-capable.
PROVIDER_SERVICE_ACCOUNTS = {
    "codex": {"account": "svc-codex", "groups": [SLOT_GROUPS[GOAL_MANAGER_ROLE], SLOT_GROUPS[WORKER_AGENT_ROLE]]},
    "claude": {"account": "svc-claude", "groups": [SLOT_GROUPS[GOAL_MANAGER_ROLE], SLOT_GROUPS[WORKER_AGENT_ROLE]]},
    "remote-aize": {"account": "svc-remote-aize", "groups": [SLOT_GROUPS[WORKER_AGENT_ROLE]]},
}

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
        "declare_session_goal_waiting",
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


def normalize_agent_role(role: str) -> str:
    normalized = str(role or "").strip()
    return ROLE_ALIASES.get(normalized, ROLE_ALIASES.get(normalized.lower(), normalized))


def slot_group_name(role: str) -> str:
    return SLOT_GROUPS.get(normalize_agent_role(role), "")


def agent_sender_endpoint(role: str, account: str | None = None) -> str:
    """Sender endpoint of a dispatched run: the appointed service user when the
    slot acts as one, otherwise the legacy role-token endpoint."""
    normalized_account = str(account or "").strip()
    if normalized_account:
        return user_endpoint(normalized_account)
    return agent_endpoint(normalize_agent_role(role))


def normalize_endpoint(value: str, *, session_id: str | None = None) -> str:
    raw = str(value or "").strip()
    if raw in AGENT_ROLES:
        return agent_endpoint(raw)
    if raw in ROLE_ALIASES and ROLE_ALIASES[raw] in AGENT_ROLES:
        return agent_endpoint(ROLE_ALIASES[raw])
    return normalize_system_endpoint(raw, session_id=session_id)
