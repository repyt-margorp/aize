from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from runtime.dispatch_queue import dispatch_priority
from runtime.message_builder import make_aize_pending_input, make_dispatch_pending_message
from runtime.panic_recovery import ensure_panic_recovery_session, panic_recovery_bootstrap_xml
from runtime.persistent_state_pkg import (
    append_history as append_user_history,
    append_pending_input,
    get_session_settings,
    list_session_agent_contacts,
    list_session_children,
    list_session_parents,
    load_pending_inputs,
    resolve_session_agent_id,
)
from wire.protocol import encode_line, utc_ts, write_jsonl

GOAL_AUDIT_HISTORY_LIMIT = 500


def existing_panic_recovery_session_id(
    runtime_root: Path,
    *,
    username: str,
    source_session_id: str,
    panic_service_id: str,
) -> tuple[str, bool]:
    for child_session_id in list_session_children(
        runtime_root,
        username=username,
        session_id=source_session_id,
    ):
        child_id = str(child_session_id or "").strip()
        if not child_id:
            continue
        child = get_session_settings(runtime_root, username=username, session_id=child_id) or {}
        if (
            str(child.get("recovery_source_session_id") or "").strip() == source_session_id
            and str(child.get("recovery_panic_service_id") or "").strip() == panic_service_id
        ):
            child_progress_state = str(
                child.get(
                    "goal_progress_state",
                    "complete" if bool(child.get("goal_completed", False)) else "in_progress",
                )
            ).strip().lower()
            is_runnable = bool(
                child.get("goal_active", False)
                and not bool(child.get("goal_completed", False))
                and child_progress_state == "in_progress"
            )
            return child_id, is_runnable
    return "", False


def queue_parent_child_panic_restart_notice(
    runtime_root: Path,
    *,
    manifest: dict[str, Any],
    process_id: str,
    router_conn: Any,
    username: str,
    child_session_id: str,
    child_label: str,
    panic_service_id: str,
    panic_event: dict[str, Any],
    recovery_session_id: str,
) -> list[str]:
    queued_parent_ids: list[str] = []
    child_settings = get_session_settings(runtime_root, username=username, session_id=child_session_id) or {}
    child_goal_text = str(child_settings.get("goal_text") or "").strip()
    summary = (
        f"Child session '{child_label or child_session_id}' entered panic recovery. "
        f"Recovery session: {recovery_session_id}"
    )
    for parent_session_id in list_session_parents(
        runtime_root,
        username=username,
        session_id=child_session_id,
    ):
        parent_settings = get_session_settings(
            runtime_root,
            username=username,
            session_id=parent_session_id,
        ) or {}
        parent_progress_state = str(
            parent_settings.get(
                "goal_progress_state",
                "complete" if bool(parent_settings.get("goal_completed", False)) else "in_progress",
            )
        ).strip().lower()
        if (
            not bool(parent_settings.get("goal_active", False))
            or bool(parent_settings.get("goal_completed", False))
            or parent_progress_state != "in_progress"
        ):
            continue
        payload = {
            "event_type": "child_session_panic",
            "parent_session_id": parent_session_id,
            "child_session_id": child_session_id,
            "child_label": child_label or child_session_id,
            "child_goal_text": child_goal_text,
            "summary": summary,
            "event": {
                "type": "child_session_panic",
                "source_session_id": child_session_id,
                "recovery_session_id": recovery_session_id,
                "panic_service_id": panic_service_id,
                "panic_event": dict(panic_event or {}),
            },
        }
        append_pending_input(
            runtime_root,
            username=username,
            session_id=parent_session_id,
            entry=make_aize_pending_input(
                kind="child_session_panic",
                role="system",
                text=json.dumps(payload, ensure_ascii=False),
            ),
        )
        append_user_history(
            runtime_root,
            username=username,
            session_id=parent_session_id,
            entry={
                "direction": "session_input",
                "kind": "child_session_panic",
                "ts": utc_ts(),
                "service_id": panic_service_id,
                "text": summary,
            },
            limit=GOAL_AUDIT_HISTORY_LIMIT,
        )
        dispatch_targets: list[str] = []
        for contact in list_session_agent_contacts(
            runtime_root,
            username=username,
            session_id=parent_session_id,
        ):
            contact_service_id = str(
                contact.get("service_id") if isinstance(contact, dict) else contact or ""
            ).strip()
            if contact_service_id and contact_service_id not in dispatch_targets:
                dispatch_targets.append(contact_service_id)
        fallback_service_id = str(parent_settings.get("service_id") or "").strip()
        if fallback_service_id and fallback_service_id not in dispatch_targets:
            dispatch_targets.append(fallback_service_id)
        for target_service_id in dispatch_targets:
            router_conn.write(
                encode_line(
                    make_dispatch_pending_message(
                        manifest=manifest,
                        from_service_id="service-svcmgr-001",
                        to_service_id=target_service_id,
                        process_id=process_id,
                        run_id=f"child-session-panic-restart-{int(time.time())}",
                        username=username,
                        session_id=parent_session_id,
                        auth_context=None,
                        reason="child_session_panic",
                        session_agent_id=resolve_session_agent_id(
                            runtime_root,
                            username=username,
                            session_id=parent_session_id,
                            service_id=target_service_id,
                        ),
                        dispatch_priority=dispatch_priority("child_session_panic"),
                    )
                )
            )
        queued_parent_ids.append(parent_session_id)
    return queued_parent_ids


def enqueue_restart_panic_recovery(
    runtime_root: Path,
    *,
    manifest: dict[str, Any],
    process_id: str,
    log_path: Path,
    router_conn: Any,
    username: str,
    source_session_id: str,
    source_label: str,
    panic_service_id: str,
    panic_event: dict[str, Any],
    preferred_provider: str,
) -> str:
    existing_recovery_session_id, existing_recovery_is_runnable = existing_panic_recovery_session_id(
        runtime_root,
        username=username,
        source_session_id=source_session_id,
        panic_service_id=panic_service_id,
    )
    if existing_recovery_session_id and not existing_recovery_is_runnable:
        write_jsonl(
            log_path,
            {
                "type": "service.restart_panic_recovery_exists",
                "ts": utc_ts(),
                "service_id": panic_service_id,
                "process_id": process_id,
                "scope": {"username": username, "session_id": source_session_id},
                "recovery_session_id": existing_recovery_session_id,
                "panic_event_type": str(panic_event.get("type") or ""),
            },
        )
        return existing_recovery_session_id
    recovery_session_id = existing_recovery_session_id
    if not recovery_session_id:
        recovery_session = ensure_panic_recovery_session(
            runtime_root,
            username=username,
            source_session_id=source_session_id,
            source_label=source_label,
            panic_service_id=panic_service_id,
            event=panic_event,
            preferred_provider=preferred_provider,
        )
        if not isinstance(recovery_session, dict):
            return ""
        recovery_session_id = str(recovery_session.get("session_id") or "").strip()
        if not recovery_session_id:
            return ""
    recovery_pending_inputs = load_pending_inputs(
        runtime_root,
        username=username,
        session_id=recovery_session_id,
    )
    already_has_recovery_input = any(
        str(item.get("kind") or "") == "panic_recovery"
        for item in recovery_pending_inputs
        if isinstance(item, dict)
    )
    if not already_has_recovery_input:
        append_pending_input(
            runtime_root,
            username=username,
            session_id=recovery_session_id,
            entry=make_aize_pending_input(
                kind="panic_recovery",
                role="system",
                text=panic_recovery_bootstrap_xml(
                    source_session_id=source_session_id,
                    source_label=source_label,
                    panic_service_id=panic_service_id,
                    event=panic_event,
                ),
            ),
        )
    if not existing_recovery_session_id:
        append_user_history(
            runtime_root,
            username=username,
            session_id=source_session_id,
            entry={
                "direction": "event",
                "ts": utc_ts(),
                "service_id": panic_service_id,
                "event_type": "service.panic_recovery_session_created",
                "text": f"Panic recovery session created: {recovery_session_id}",
                "event": {
                    "type": "service.panic_recovery_session_created",
                    "source_session_id": source_session_id,
                    "recovery_session_id": recovery_session_id,
                    "panic_service_id": panic_service_id,
                    "panic_event": dict(panic_event or {}),
                },
            },
            limit=GOAL_AUDIT_HISTORY_LIMIT,
        )
    dispatch_message = make_dispatch_pending_message(
        manifest=manifest,
        from_service_id="service-svcmgr-001",
        to_service_id=panic_service_id,
        process_id=process_id,
        run_id=f"restart-recovery-{int(time.time())}",
        username=username,
        session_id=recovery_session_id,
        auth_context=None,
        reason="restart_recovery",
        session_agent_id=resolve_session_agent_id(
            runtime_root,
            username=username,
            session_id=recovery_session_id,
            service_id=panic_service_id,
        ),
        dispatch_priority=dispatch_priority("panic_recovery"),
    )
    router_conn.write(encode_line(dispatch_message))
    queued_parent_ids = queue_parent_child_panic_restart_notice(
        runtime_root,
        manifest=manifest,
        process_id=process_id,
        router_conn=router_conn,
        username=username,
        child_session_id=source_session_id,
        child_label=source_label,
        panic_service_id=panic_service_id,
        panic_event=panic_event,
        recovery_session_id=recovery_session_id,
    )
    write_jsonl(
        log_path,
        {
            "type": "service.restart_recovery_enqueued",
            "ts": utc_ts(),
            "service_id": panic_service_id,
            "process_id": process_id,
            "scope": {"username": username, "session_id": source_session_id},
            "recovery_session_id": recovery_session_id,
            "panic_event_type": str(panic_event.get("type") or ""),
            "existing_recovery": bool(existing_recovery_session_id),
            "queued_parent_session_ids": queued_parent_ids,
        },
    )
    return recovery_session_id
