from __future__ import annotations

from typing import Any, Callable

from runtime.dispatch_queue import dispatch_priority
from runtime.message_builder import make_dispatch_pending_message


def communication_dispatch_plan(
    *,
    session_id: str,
    interactive_service_id: str | None,
    worker_service_id: str | None,
    goal_manager_service_id: str | None,
    forwarded_session_id: str | None,
    forwarded_service_id: str | None,
    forwarded_dispatch_reason: str | None,
) -> list[dict[str, str]]:
    plan: list[dict[str, str]] = []
    normalized_session_id = str(session_id or "").strip()
    normalized_interactive_service_id = str(interactive_service_id or "").strip()
    normalized_worker_service_id = str(worker_service_id or "").strip()
    normalized_goal_manager_service_id = str(goal_manager_service_id or "").strip()
    normalized_forwarded_session_id = str(forwarded_session_id or "").strip()
    normalized_forwarded_service_id = str(forwarded_service_id or "").strip()
    normalized_forwarded_reason = str(forwarded_dispatch_reason or "http_prompt").strip() or "http_prompt"
    if normalized_interactive_service_id and normalized_session_id:
        plan.append(
            {
                "channel": "interactive",
                "service_id": normalized_interactive_service_id,
                "session_id": normalized_session_id,
                "reason": "http_user_dialogue",
            }
        )
    if normalized_worker_service_id and normalized_session_id:
        plan.append(
            {
                "channel": "worker",
                "service_id": normalized_worker_service_id,
                "session_id": normalized_session_id,
                "reason": "interactive_worker_request",
            }
        )
    if normalized_goal_manager_service_id and normalized_session_id:
        plan.append(
            {
                "channel": "goal_manager",
                "service_id": normalized_goal_manager_service_id,
                "session_id": normalized_session_id,
                "reason": "goal_manager_review",
            }
        )
    if normalized_forwarded_session_id and normalized_forwarded_service_id:
        plan.append(
            {
                "channel": "forwarded",
                "service_id": normalized_forwarded_service_id,
                "session_id": normalized_forwarded_session_id,
                "reason": normalized_forwarded_reason,
            }
        )
    return plan


def send_http_dispatch_plan(
    *,
    dispatch_plan: list[dict[str, str]],
    manifest: dict[str, Any],
    from_service_id: str,
    process_id: str,
    run_id: str,
    worker_request_id: str,
    username: str,
    auth_context: dict[str, Any] | None,
    selected_agent_profile: dict[str, Any] | None,
    slot_agent_id: Callable[[str, str, str], str],
    resolve_goal_manager_agent_id: Callable[[str, str], str],
    send_router_control: Callable[[dict[str, Any]], bool],
) -> tuple[bool, str]:
    worker_dispatch_queued = False
    dispatch_error = ""
    queued_messages: list[tuple[str, dict[str, Any]]] = []
    for dispatch_step in dispatch_plan:
        channel = str(dispatch_step.get("channel") or "")
        target_service_id = str(dispatch_step.get("service_id") or "")
        target_session_id = str(dispatch_step.get("session_id") or "")
        reason = str(dispatch_step.get("reason") or "")
        session_agent_id = None
        agent_profile = None
        if channel in {"interactive", "agent"}:
            agent_profile = selected_agent_profile
        elif channel == "worker":
            session_agent_id = slot_agent_id(target_service_id, target_session_id, "worker_agent")
            worker_dispatch_queued = True
        elif channel == "goal_manager":
            session_agent_id = resolve_goal_manager_agent_id(target_service_id, target_session_id)
        queued_messages.append(
            (
                channel,
                make_dispatch_pending_message(
                    manifest=manifest,
                    from_service_id=from_service_id,
                    to_service_id=target_service_id,
                    process_id=process_id,
                    run_id=(worker_request_id or run_id) if channel == "worker" else run_id,
                    username=username,
                    session_id=target_session_id,
                    auth_context=auth_context,
                    reason=reason,
                    session_agent_id=session_agent_id,
                    agent_profile=agent_profile,
                    dispatch_priority=dispatch_priority(reason),
                ),
            )
        )
    indexed_messages = list(enumerate(queued_messages))
    ordered_messages = sorted(
        indexed_messages,
        key=lambda item: (
            -dispatch_priority((item[1][1].get("payload") or {}).get("reason")),
            item[0],
        ),
    )
    for _, (channel, message) in ordered_messages:
        if not send_router_control(message):
            dispatch_error = dispatch_error or f"router_control_injection_failed:{channel}"
    return worker_dispatch_queued, dispatch_error
