from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from goal.store import Store
from goal.defs import (
    AGENT_ROLES,
    SLOT_ROLE_BY_GROUP,
    normalize_agent_role,
    slot_group_name,
)
from kernel.defs import SESSION_RECIPIENT, USER_CONSOLE_RECIPIENT, StoreError


def runtime_context() -> dict[str, str]:
    """The external agent subprocess contract. ``AIZE_AGENT_GROUP`` plus
    ``AIZE_AGENT_USER`` are canonical; ``AIZE_AGENT_ROLE`` stays accepted
    through the alias window."""
    role_token = os.environ.get("AIZE_AGENT_ROLE", "").strip()
    group = os.environ.get("AIZE_AGENT_GROUP", "").strip()
    if not group:
        if role_token:
            group = slot_group_name(role_token)
        else:
            raise StoreError(
                "AIZE_AGENT_GROUP (or legacy AIZE_AGENT_ROLE) is required for agent_api"
            )
    if group not in SLOT_ROLE_BY_GROUP:
        raise StoreError(
            f"AIZE_AGENT_GROUP must be one of {', '.join(sorted(SLOT_ROLE_BY_GROUP))}: {group}"
        )
    agent_user = os.environ.get("AIZE_AGENT_USER", "").strip()
    agent_role = normalize_agent_role(role_token or group)
    return {
        "state_root": _required_env("AIZE_STATE_ROOT"),
        "session_id": _required_env("AIZE_SESSION_ID"),
        "agent_role": agent_role,
        "agent_group": group,
        "agent_user": agent_user,
        "run_id": os.environ.get("AIZE_RUN_ID", ""),
    }


def send_message(
    recipient: str,
    body: str,
    *,
    recipient_endpoint_id: str | None = None,
    files: list[dict[str, Any]] | None = None,
    worker_request: bool = False,
    wake_event: str | None = None,
) -> dict[str, Any]:
    context = runtime_context()
    sender = context["agent_user"] or context["agent_role"]
    if not context["agent_user"] and sender not in AGENT_ROLES:
        raise StoreError(
            f"AIZE_AGENT_GROUP/AIZE_AGENT_USER decline this sender identity: {sender}"
        )
    store = Store(Path(context["state_root"]))
    return store.append_runtime_message(
        context["session_id"],
        sender=sender,
        recipient=recipient,
        body=body,
        group=context["agent_group"],
        run_id=context["run_id"] or None,
        recipient_endpoint_id=recipient_endpoint_id,
        files=files,
        worker_request=worker_request,
        wake_event=wake_event,
    )


def send_user_console_message(
    body: str,
    *,
    recipient_endpoint_id: str | None = None,
) -> dict[str, Any]:
    return send_message(
        USER_CONSOLE_RECIPIENT,
        body,
        recipient_endpoint_id=recipient_endpoint_id,
    )


def send_session_message(body: str, *, wake_event: str | None = None) -> dict[str, Any]:
    return send_message(SESSION_RECIPIENT, body, wake_event=wake_event)


def send_worker_request(body: str) -> dict[str, Any]:
    return send_message(SESSION_RECIPIENT, body, worker_request=True)


def get_session_goal_dag(*, include_descendants: bool = True) -> dict[str, Any]:
    """Read the current Session's ancestor/descendant Goal context without writing a Message."""
    context = runtime_context()
    if context["agent_group"] not in SLOT_ROLE_BY_GROUP:
        raise StoreError(
            f"AIZE_AGENT_GROUP is not allowed to read Session context: {context['agent_group']}"
        )
    return Store(Path(context["state_root"])).session_goal_dag(
        context["session_id"],
        include_descendants=include_descendants,
    )


def set_goal_completion_state(
    completion_state: str,
    reason: str,
    *,
    schedule_parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    context = runtime_context()
    return Store(Path(context["state_root"])).set_goal_completion_state_from_runtime(
        context["session_id"],
        completion_state=completion_state,
        reason=reason,
        actor=context["agent_user"] or context["agent_role"],
        run_id=context["run_id"] or None,
        schedule_parameters=schedule_parameters,
    )


def set_goal_waiting(wait: dict[str, Any], *, note: str = "") -> dict[str, Any]:
    """Record the current Session's bounded Waiting decision without creating Worker work."""
    context = runtime_context()
    return Store(Path(context["state_root"])).set_goal_waiting_from_runtime(
        context["session_id"],
        wait=wait,
        actor=context["agent_user"] or context["agent_role"],
        run_id=context["run_id"] or None,
        reason=note,
    )


def schedule_next_unit_run(parameters: dict[str, Any] | None = None) -> dict[str, Any]:
    context = runtime_context()
    return Store(Path(context["state_root"])).schedule_next_unit_run_from_session(
        context["session_id"],
        call_parameters=parameters or {},
        actor=context["agent_user"] or context["agent_role"],
        run_id=context["run_id"] or None,
    )


def set_next_unit_run_at(next_run_at: str, *, note: str = "") -> dict[str, Any]:
    return schedule_next_unit_run({"next_run_at": next_run_at, "note": note})


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise StoreError(f"{name} is required for agent_api")
    return value
