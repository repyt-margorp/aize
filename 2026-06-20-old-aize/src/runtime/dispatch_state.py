from __future__ import annotations

from pathlib import Path
from typing import Any

from runtime.dispatch_policy import slot_agent_id
from runtime.persistent_state_pkg import (
    load_pending_inputs,
    load_service_pending_inputs,
    resolve_session_agent_id,
)


def dispatch_target_agent_id(
    explicit_agent_id: Any,
    *,
    runtime_root: Path,
    username: str,
    session_id: str,
    service_id: str,
    provider_session_slot: str,
) -> str:
    explicit = str(explicit_agent_id or "").strip()
    if explicit:
        return explicit
    if provider_session_slot in {"interactive_agent", "worker_agent"}:
        return slot_agent_id(service_id, session_id, provider_session_slot)
    return str(
        resolve_session_agent_id(
            runtime_root,
            username=username,
            session_id=session_id,
            service_id=service_id,
        )
    ).strip()


def post_turn_followup_pending_state(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    service_id: str,
    provider_session_slot: str,
    explicit_agent_id: Any = None,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], bool]:
    target_agent_id = dispatch_target_agent_id(
        explicit_agent_id,
        runtime_root=runtime_root,
        username=username,
        session_id=session_id,
        service_id=service_id,
        provider_session_slot=provider_session_slot,
    )
    session_pending_inputs = load_pending_inputs(
        runtime_root,
        username=username,
        session_id=session_id,
    )
    service_pending_inputs = load_service_pending_inputs(
        runtime_root,
        service_id=service_id,
        agent_id=target_agent_id,
        username=username,
        session_id=session_id,
    )
    has_actionable_pending = any(
        str(item.get("kind", "")) != "turn_completed"
        for item in list(session_pending_inputs) + list(service_pending_inputs)
    )
    return target_agent_id, session_pending_inputs, service_pending_inputs, has_actionable_pending
