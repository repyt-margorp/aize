from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from store import Store
from store_defs import (
    AGENT_ROLES,
    SESSION_RECIPIENT,
    USER_CONSOLE_RECIPIENT,
    StoreError,
)


def runtime_context() -> dict[str, str]:
    return {
        "state_root": _required_env("AIZE_STATE_ROOT"),
        "session_id": _required_env("AIZE_SESSION_ID"),
        "agent_role": _required_env("AIZE_AGENT_ROLE"),
        "run_id": os.environ.get("AIZE_RUN_ID", ""),
    }


def send_message(
    recipient: str,
    body: str,
    *,
    recipient_endpoint_id: str | None = None,
    files: list[dict[str, Any]] | None = None,
    worker_request: bool = False,
) -> dict[str, Any]:
    context = runtime_context()
    sender = context["agent_role"]
    if sender not in AGENT_ROLES:
        raise StoreError(f"AIZE_AGENT_ROLE is not allowed to send messages: {sender}")
    store = Store(Path(context["state_root"]))
    return store.append_runtime_message(
        context["session_id"],
        sender=sender,
        recipient=recipient,
        body=body,
        run_id=context["run_id"] or None,
        recipient_endpoint_id=recipient_endpoint_id,
        files=files,
        worker_request=worker_request,
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


def send_session_message(body: str) -> dict[str, Any]:
    return send_message(SESSION_RECIPIENT, body)


def send_worker_request(body: str) -> dict[str, Any]:
    return send_message(SESSION_RECIPIENT, body, worker_request=True)


def set_next_unit_run_at(next_run_at: str, *, note: str = "") -> dict[str, Any]:
    context = runtime_context()
    sender = context["agent_role"]
    store = Store(Path(context["state_root"]))
    return store.set_next_unit_run_at_from_session(
        context["session_id"],
        next_run_at=next_run_at,
        note=note,
        actor=sender,
        run_id=context["run_id"] or None,
    )


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise StoreError(f"{name} is required for agent_api")
    return value
