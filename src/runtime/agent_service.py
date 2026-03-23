from __future__ import annotations

import html
import threading
import uuid
import re
from pathlib import Path
from typing import Any, Callable

from kernel.lifecycle import get_process_record, register_process, update_process_fields
from kernel.registry import update_service_process
from runtime.compaction import (
    GOAL_AUDIT_HISTORY_LIMIT,
    maybe_auto_compact_claude_session,
    maybe_auto_compact_codex_session,
    resolve_session_auto_compact_threshold,
    wait_for_service_record,
)
from runtime.event_log import (
    emit_turn_completed_event,
    make_history_event_entry,
)
from runtime.goal_audit import (
    default_goal_continue_xml,
    goal_audit_should_enqueue_agent_followup,
    goal_followup_dispatch_targets,
    run_goal_audit,
)
from runtime.goal_persist import (
    handle_goal_manager_compact_request,
    persist_goal_audit_completion,
)
from runtime.panic_recovery import (
    ensure_panic_recovery_session,
    panic_recovery_bootstrap_xml,
)
from runtime.message_builder import (
    batch_has_input_kind,
    build_aize_input_batch_xml,
    build_outgoing_event_message,
    build_outgoing_message,
    dispatch_pending_opens_visible_turn,
    make_aize_pending_input,
    make_dispatch_pending_message,
    maybe_release_session_provider,
    resolve_conversation_scope,
    resolve_payload_text,
)
from runtime.persistent_state import (
    append_history as append_user_history,
    append_goal_manager_pending_input,
    append_pending_input,
    append_service_pending_input,
    complete_session_child,
    create_child_conversation_session,
    drain_goal_manager_pending_inputs,
    drain_pending_inputs,
    drain_service_pending_inputs,
    get_history as get_user_history,
    get_session_settings,
    active_agent_priority,
    lease_session_service,
    load_agent_audit_state,
    load_codex_session,
    load_claude_session,
    load_pending_inputs,
    load_service_pending_inputs,
    list_session_agent_contacts,
    record_session_agent_contact,
    release_session_service,
    resolve_session_agent_id,
    read_json_file,
    schedule_session_auto_resume,
    save_agent_audit_state,
    save_claude_session,
    save_codex_session,
    session_goal_manager_state_path,
    session_dir,
    session_timeline_path,
    update_goal_manager_review_cursor,
    update_session_goal_flags,
    write_json_file,
)
from runtime.providers import run_claude, run_codex
from runtime.service_control import (
    build_prompt,
    parse_service_response,
)
from wire.protocol import (
    decode_line,
    encode_line,
    make_message,
    message_meta_get,
    message_set_meta,
    utc_ts,
    write_jsonl,
)


_USAGE_LIMIT_RETRY_RE = re.compile(r"try again at ([0-9]{1,2}:[0-9]{2}\s*[AP]M)", re.IGNORECASE)


def _is_usage_limit_error_text(text: str) -> bool:
    normalized = str(text or "").lower()
    return "usage limit" in normalized or "rate limit" in normalized or "too many requests" in normalized


def _retry_after_seconds_from_error_text(text: str) -> int | None:
    match = _USAGE_LIMIT_RETRY_RE.search(str(text or ""))
    if not match:
        return None
    return 6 * 60 * 60


def build_panic_recovery_parent_return_xml(
    source_session_id: str,
    recovery_session_id: str,
    panic_service_id: str,
) -> str:
    return "".join(
        [
            "<aize_panic_recovery_parent_resume>",
            f"<source_session_id>{html.escape(source_session_id)}</source_session_id>",
            f"<recovery_session_id>{html.escape(recovery_session_id)}</recovery_session_id>",
            f"<panic_service_id>{html.escape(panic_service_id)}</panic_service_id>",
            "<instruction>Resume parent session from panic recovery completion.</instruction>",
            "</aize_panic_recovery_parent_resume>",
        ]
    )


def _parent_resume_validated_for_recovery_completion(
    *,
    runtime_root: Path,
    username: str,
    recovery_session_settings: dict[str, Any],
) -> tuple[bool, str]:
    if str(recovery_session_settings.get("session_group") or "").strip().lower() != "error":
        return True, ""
    parent_session_id = str(recovery_session_settings.get("parent_session_id") or "").strip()
    if not parent_session_id:
        return True, ""
    recovery_created_at = str(recovery_session_settings.get("created_at") or "").strip()
    parent_history = get_user_history(runtime_root, username=username, session_id=parent_session_id)
    for entry in parent_history:
        ts = str(entry.get("ts") or "")
        if recovery_created_at and ts <= recovery_created_at:
            continue
        direction = str(entry.get("direction") or "")
        event_type = str(entry.get("event_type") or "")
        if direction == "in":
            return True, ""
        if event_type in {"agent.turn_started", "turn.completed"}:
            return True, ""
    return False, "parent_session_not_resumed_after_recovery"


def _session_completion_override_xml(*, reason: str, session_dir_path: str, timeline_path: str) -> str:
    return "\n".join(
        [
            "<aize_completion_override>",
            f"  <reason>{html.escape(reason)}</reason>",
            "  <instruction>Do not treat this session as completed yet. The completion validator rejected the completed state. Continue only the work required to satisfy the validator, then verify again.</instruction>",
            f"  <session_dir>{html.escape(session_dir_path)}</session_dir>",
            f"  <timeline_path>{html.escape(timeline_path)}</timeline_path>",
            "</aize_completion_override>",
        ]
    )


def maybe_dispatch_panic_recovery_parent_resume(
    *,
    incoming_text: str,
    runtime_root: Path,
    manifest: dict[str, Any],
    service_id: str,
    process_id: str,
    log_path: Path,
    send_tx: Callable[[dict[str, Any]], None],
    scope_username: str,
    scope_session_id: str,
    session_settings: dict[str, Any],
) -> None:
    if not batch_has_input_kind(incoming_text, "panic_recovery"):
        return

    parent_session_id = str(session_settings.get("parent_session_id") or "").strip()
    if not parent_session_id or parent_session_id == scope_session_id:
        return

    parent_session_settings = get_session_settings(
        runtime_root,
        username=scope_username,
        session_id=parent_session_id,
    )
    if not isinstance(parent_session_settings, dict):
        return
    dispatch_service_id = str(
        parent_session_settings.get("service_id") or session_settings.get("service_id") or service_id
    )
    existing_parent_inputs = load_pending_inputs(
        runtime_root,
        username=scope_username,
        session_id=parent_session_id,
    )
    recovery_session_id = scope_session_id
    for item in existing_parent_inputs:
        if str(item.get("kind") or "") != "restart_resume":
            continue
        if recovery_session_id in str(item.get("text") or ""):
            return

    append_pending_input(
        runtime_root,
        username=scope_username,
        session_id=parent_session_id,
        entry=make_aize_pending_input(
            kind="restart_resume",
            role="system",
            text=build_panic_recovery_parent_return_xml(
                source_session_id=parent_session_id,
                recovery_session_id=recovery_session_id,
                panic_service_id=dispatch_service_id,
            ),
        ),
    )
    dispatch_message = make_dispatch_pending_message(
        manifest=manifest,
        from_service_id=service_id,
        to_service_id=dispatch_service_id,
        process_id=process_id,
        run_id=f"panic-recovery-resume-{uuid.uuid4().hex[:8]}",
        username=scope_username,
        session_id=parent_session_id,
        auth_context=None,
        reason="panic_recovery_parent_resume",
        session_agent_id=resolve_session_agent_id(
            runtime_root,
            username=scope_username,
            session_id=parent_session_id,
            service_id=dispatch_service_id,
        ),
    )
    send_tx(dispatch_message)
    write_jsonl(
        log_path,
        {
            "type": "service.panic_recovery_parent_resume_dispatched",
            "ts": utc_ts(),
            "service_id": service_id,
            "process_id": process_id,
            "scope": {"username": scope_username, "session_id": scope_session_id},
            "parent_session_id": parent_session_id,
            "recovery_session_id": recovery_session_id,
            "panic_service_id": dispatch_service_id,
            "dispatch_target_session_id": parent_session_id,
        },
    )


def _goal_update_xml(
    *,
    goal_id: str,
    goal_text: str,
    previous_goal_id: str = "",
    previous_goal_text: str = "",
) -> str:
    lines = ["<aize_goal_update>"]
    if previous_goal_id:
        lines.append(f"  <previous_goal_id>{html.escape(previous_goal_id)}</previous_goal_id>")
    if previous_goal_text:
        lines.append(f"  <previous_goal>{html.escape(previous_goal_text)}</previous_goal>")
    if goal_id:
        lines.append(f"  <goal_id>{html.escape(goal_id)}</goal_id>")
    lines.append(f"  <goal_text>{html.escape(goal_text)}</goal_text>")
    lines.append("  <instruction>Review the active goal and continue work toward it until GoalManager can mark it completed.</instruction>")
    lines.append("</aize_goal_update>")
    return "\n".join(lines)


def run_agent_service(
    *,
    runtime_root: Path,
    manifest: dict[str, Any],
    self_service: dict[str, Any],
    process_id: str,
    log_path: Path,
    router_conn: Any = None,
) -> int:
    from kernel.ipc import connect_to_router, RouterConnection
    service_id = self_service["service_id"]
    if router_conn is None:
        router_conn = connect_to_router(runtime_root, service_id)
    config = dict(self_service.get("config", {}))
    history_limit = int(config.get("history_limit", 500))
    reply_count = 0
    reply_count_lock = threading.Lock()
    done_sent = threading.Event()
    scope_locks: dict[str, threading.Lock] = {}
    scope_locks_guard = threading.Lock()
    workers: list[threading.Thread] = []

    def send_tx(message_obj: dict[str, Any]) -> None:
        router_conn.write(encode_line(message_obj))

    class LockedTxHandle:
        def write(self, data: str) -> int:
            router_conn.write(data)
            return len(data)

        def flush(self) -> None:
            pass

    def scope_lock_for(username: str | None, session_id: str | None) -> threading.Lock:
        key = f"{username}::{session_id}" if username and session_id else "__global__"
        with scope_locks_guard:
            lock = scope_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                scope_locks[key] = lock
            return lock

    def spawn_panic_recovery(
        *,
        username: str,
        session_id: str,
        panic_event: dict[str, Any],
        panic_service_id: str,
    ) -> dict[str, Any] | None:
        session_settings = get_session_settings(runtime_root, username=username, session_id=session_id) or {}
        session_label = str(session_settings.get("label") or session_id)
        preferred_provider = "claude" if "claude" in panic_service_id else "codex"
        recovery_session = ensure_panic_recovery_session(
            runtime_root,
            username=username,
            source_session_id=session_id,
            source_label=session_label,
            panic_service_id=panic_service_id,
            event=panic_event,
            preferred_provider=preferred_provider,
        )
        if not isinstance(recovery_session, dict):
            return None
        recovery_session_id = str(recovery_session.get("session_id") or "").strip()
        if not recovery_session_id:
            return None
        bootstrap_xml = panic_recovery_bootstrap_xml(
            source_session_id=session_id,
            source_label=session_label,
            panic_service_id=panic_service_id,
            event=panic_event,
        )
        append_pending_input(
            runtime_root,
            username=username,
            session_id=recovery_session_id,
            entry=make_aize_pending_input(
                kind="panic_recovery",
                role="system",
                text=bootstrap_xml,
            ),
        )
        append_user_history(
            runtime_root,
            username=username,
            session_id=session_id,
            entry={
                "direction": "event",
                "ts": utc_ts(),
                "service_id": panic_service_id,
                "event_type": "service.panic_recovery_session_created",
                "text": f"Panic recovery session created: {recovery_session_id}",
                "event": {
                    "type": "service.panic_recovery_session_created",
                    "source_session_id": session_id,
                    "recovery_session_id": recovery_session_id,
                    "panic_service_id": panic_service_id,
                    "panic_event": dict(panic_event or {}),
                },
            },
            limit=GOAL_AUDIT_HISTORY_LIMIT,
        )
        dispatch_message = make_dispatch_pending_message(
            manifest=manifest,
            from_service_id=service_id,
            to_service_id=panic_service_id or service_id,
            process_id=process_id,
            run_id=f"panic-recovery-{uuid.uuid4().hex[:8]}",
            username=username,
            session_id=recovery_session_id,
            auth_context=None,
            reason="panic_recovery",
        )
        send_tx(dispatch_message)
        return recovery_session

    def _pool_for_kind_from_manifest(kind: str) -> list[str]:
        """Derive service pool for a provider kind from the manifest."""
        return [
            s["service_id"]
            for s in manifest.get("services", [])
            if isinstance(s.get("service_id"), str) and s.get("kind") == kind
        ]

    def maybe_spawn_failure_recovery(
        *,
        username: str | None,
        session_id: str | None,
        failure_event: dict[str, Any],
        failure_service_id: str,
    ) -> dict[str, Any] | None:
        if not (isinstance(username, str) and isinstance(session_id, str) and username and session_id):
            return None
        error_text = str(
            failure_event.get("error")
            or failure_event.get("reason")
            or failure_event.get("text")
            or ""
        ).strip()

        # Determine provider kind of the failed service
        session_settings = get_session_settings(runtime_root, username=username, session_id=session_id) or {}
        agent_priority = active_agent_priority(session_settings.get("agent_priority"))

        # Determine which kind the failed service is
        failed_kind = str(
            next(
                (s.get("kind") for s in manifest.get("services", []) if s.get("service_id") == failure_service_id),
                "codex" if "codex" in failure_service_id.lower() else "claude",
            )
        )

        if _is_usage_limit_error_text(error_text):
            # Mark the failed service as panic
            save_agent_audit_state(
                runtime_root,
                service_id=failure_service_id,
                username=username,
                session_id=session_id,
                audit_state="panic",
            )

            # Try next providers in agent_priority order before creating a recovery session.
            # Recovery sessions should only be spawned when ALL providers are exhausted.
            tried_kinds = {failed_kind}
            for provider in agent_priority:
                if provider in tried_kinds:
                    continue
                tried_kinds.add(provider)
                fallback_pool = _pool_for_kind_from_manifest(provider)
                if not fallback_pool:
                    continue
                # Release current service binding and try next pool
                release_session_service(runtime_root, username=username, session_id=session_id)
                next_svc = lease_session_service(
                    runtime_root,
                    username=username,
                    session_id=session_id,
                    pool_service_ids=fallback_pool,
                )
                if not next_svc:
                    continue
                # Fallback available: re-enqueue goal and dispatch to new provider
                goal_text = str(session_settings.get("goal_text") or "").strip()
                active_goal_id = str(session_settings.get("active_goal_id") or session_settings.get("goal_id") or "").strip()
                if goal_text:
                    append_pending_input(
                        runtime_root,
                        username=username,
                        session_id=session_id,
                        entry=make_aize_pending_input(
                            kind="goal_update",
                            role="system",
                            text=_goal_update_xml(goal_id=active_goal_id, goal_text=goal_text),
                        ),
                    )
                fallback_dispatch = make_dispatch_pending_message(
                    manifest=manifest,
                    from_service_id=service_id,
                    to_service_id=next_svc,
                    process_id=process_id,
                    run_id=f"provider-fallback-{uuid.uuid4().hex[:8]}",
                    username=username,
                    session_id=session_id,
                    auth_context=None,
                    reason="provider_fallback",
                )
                send_tx(fallback_dispatch)
                append_user_history(
                    runtime_root,
                    username=username,
                    session_id=session_id,
                    entry={
                        "direction": "event",
                        "ts": utc_ts(),
                        "service_id": failure_service_id,
                        "event_type": "service.provider_fallback",
                        "text": f"Rate limit: {failed_kind} exhausted, switched to {provider} ({next_svc})",
                        "event": {
                            "type": "service.provider_fallback",
                            "from_service_id": failure_service_id,
                            "to_service_id": next_svc,
                            "from_provider": failed_kind,
                            "to_provider": provider,
                            "reason": "rate_limit",
                        },
                    },
                    limit=GOAL_AUDIT_HISTORY_LIMIT,
                )
                return None  # No recovery session needed; dispatched to fallback

            # All providers in agent_priority exhausted — fall back to recovery session
            update_session_goal_flags(
                runtime_root,
                username=username,
                session_id=session_id,
                goal_completed=True,
                goal_progress_state="complete",
            )
            schedule_session_auto_resume(
                runtime_root,
                username=username,
                session_id=session_id,
                reason="rate_limit",
                error_text=error_text,
                retry_after_seconds=_retry_after_seconds_from_error_text(error_text),
                mark_completed=True,
            )
            append_user_history(
                runtime_root,
                username=username,
                session_id=session_id,
                entry={
                    "direction": "event",
                    "ts": utc_ts(),
                    "service_id": failure_service_id,
                    "event_type": "service.auto_resume_scheduled",
                    "text": "All providers rate-limited; auto resume scheduled",
                    "event": {
                        "type": "service.auto_resume_scheduled",
                        "reason": "rate_limit",
                        "error": error_text,
                    },
                },
                limit=GOAL_AUDIT_HISTORY_LIMIT,
            )
            return spawn_panic_recovery(
                username=username,
                session_id=session_id,
                panic_event={
                    **dict(failure_event or {}),
                    "type": str(failure_event.get("type") or "service.worker_failed"),
                    "deferred_reason": "rate_limit",
                    "auto_resume": {
                        "enabled": True,
                        "reason": "rate_limit",
                    },
                },
                panic_service_id=failure_service_id,
            )
        # Non-rate-limit failure: mark panic and create recovery session
        save_agent_audit_state(
            runtime_root,
            service_id=failure_service_id,
            username=username,
            session_id=session_id,
            audit_state="panic",
        )
        update_session_goal_flags(
            runtime_root,
            username=username,
            session_id=session_id,
            goal_completed=False,
            goal_progress_state="in_progress",
        )
        return spawn_panic_recovery(
            username=username,
            session_id=session_id,
            panic_event=failure_event,
            panic_service_id=failure_service_id,
        )

    def process_prompt_message(message: dict[str, Any], *, reply_index: int) -> None:
        dispatch_pending = message.get("type") == "dispatch_pending"
        sender_service_id = str(
            message_meta_get(message, "reply_to_service_id")
            if dispatch_pending and message_meta_get(message, "reply_to_service_id")
            else message["from"]
        )
        if sender_service_id.startswith("service-"):
            peer_service = wait_for_service_record(runtime_root, sender_service_id)
        else:
            peer_service = {
                "service_id": sender_service_id,
                "display_name": sender_service_id,
            }
        scope_username, scope_session_id = resolve_conversation_scope(message)
        def append_scoped_history(entry: dict[str, Any], *, limit: int) -> None:
            if not (scope_username and scope_session_id):
                return
            append_user_history(
                runtime_root,
                username=scope_username,
                session_id=scope_session_id,
                entry=entry,
                limit=limit,
            )
            event_message = build_outgoing_event_message(
                runtime_root=runtime_root,
                manifest=manifest,
                from_node_id=manifest["node_id"],
                from_service_id=service_id,
                to_node_id=manifest["node_id"],
                to_service_id=sender_service_id,
                process_id=process_id,
                run_id=str(message_meta_get(message, "run_id") or ""),
                entry=entry,
                username=scope_username,
                session_id=scope_session_id,
            )
            send_tx(event_message)
        if dispatch_pending:
            if not (scope_username and scope_session_id):
                return
            target_agent_id = str(
                message_meta_get(message, "session_agent_id")
                or resolve_session_agent_id(
                    runtime_root,
                    username=scope_username,
                    session_id=scope_session_id,
                    service_id=service_id,
                )
            ).strip()
            # Quick pre-check (peek, not drain) to skip lock contention for obvious noops
            session_pending_inputs = load_pending_inputs(
                runtime_root,
                username=scope_username,
                session_id=scope_session_id,
            )
            service_pending_inputs = load_service_pending_inputs(
                runtime_root,
                service_id=service_id,
                agent_id=target_agent_id,
                username=scope_username,
                session_id=scope_session_id,
            )
            if not (session_pending_inputs or service_pending_inputs):
                write_jsonl(
                    log_path,
                    {
                        "type": "service.dispatch_pending_noop",
                        "ts": utc_ts(),
                        "service_id": service_id,
                        "process_id": process_id,
                        "reply_index": reply_index,
                        "scope": {"username": scope_username, "session_id": scope_session_id},
                    },
                )
                return
        try:
            with scope_lock_for(scope_username, scope_session_id):
                # Drain pending inputs inside the scope lock to prevent a race where two
                # dispatch_pending messages both see a non-empty queue and each launch Codex.
                if dispatch_pending:
                    pending_inputs = drain_pending_inputs(
                        runtime_root,
                        username=scope_username,
                        session_id=scope_session_id,
                    )
                    service_pending_inputs = drain_service_pending_inputs(
                        runtime_root,
                        service_id=service_id,
                        agent_id=target_agent_id,
                        username=scope_username,
                        session_id=scope_session_id,
                    )
                    pending_inputs.extend(service_pending_inputs)
                    if not pending_inputs:
                        write_jsonl(
                            log_path,
                            {
                                "type": "service.dispatch_pending_noop",
                                "ts": utc_ts(),
                                "service_id": service_id,
                                "process_id": process_id,
                                "reply_index": reply_index,
                                "scope": {"username": scope_username, "session_id": scope_session_id},
                            },
                        )
                        return
                    # Goal-complete dispatch gate: drop inputs silently when the active goal
                    # is already complete so no further feedback reaches the agent.
                    _dispatch_session_settings = get_session_settings(
                        runtime_root, username=scope_username, session_id=scope_session_id
                    ) or {}
                    _dispatch_goal_active = bool(_dispatch_session_settings.get("goal_active", False))
                    _dispatch_goal_progress = str(
                        _dispatch_session_settings.get(
                            "goal_progress_state",
                            "complete" if bool(_dispatch_session_settings.get("goal_completed", False)) else "in_progress",
                        )
                    ).strip().lower()
                    if _dispatch_goal_active and _dispatch_goal_progress == "complete":
                        write_jsonl(
                            log_path,
                            {
                                "type": "service.dispatch_pending_goal_complete_skip",
                                "ts": utc_ts(),
                                "service_id": service_id,
                                "process_id": process_id,
                                "reply_index": reply_index,
                                "scope": {"username": scope_username, "session_id": scope_session_id},
                                "dropped_input_count": len(pending_inputs),
                            },
                        )
                        return
                    batch_instruction = (
                        "Respond to the queued talk inputs in order, prioritizing the latest user-visible requirement while preserving relevant pending system context."
                    )
                    if batch_has_input_kind(pending_inputs, "restart_resume"):
                        batch_instruction += (
                            " If a restart-resume input is present, treat it as an execution-resume directive: continue the interrupted work immediately and do not consume the turn with a status-only acknowledgment."
                        )
                    incoming_text = build_aize_input_batch_xml(
                        sender_display_name=str(peer_service["display_name"]),
                        username=scope_username,
                        session_id=scope_session_id,
                        inputs=pending_inputs,
                        instruction=batch_instruction,
                    )
                else:
                    incoming_text = resolve_payload_text(runtime_root, message)
                is_user_turn = batch_has_input_kind(incoming_text, "user_message")
                write_jsonl(
                    log_path,
                    {
                        "type": "message.in",
                        "ts": utc_ts(),
                        "service_id": service_id,
                        "process_id": process_id,
                        "reply_index": reply_index,
                        "payload_mode": "ref" if message.get("payload_ref") else "inline",
                        "message_type": str(message.get("type", "prompt")),
                        "message": message,
                    },
                )

                prompt = build_prompt(self_service, peer_service, incoming_text, reply_index)
                next_session_id: str | None = None
                provider_events: list[dict[str, Any]] = []

                # Bulletin-board signal: notify the session that this agent has started responding
                if (
                    dispatch_pending
                    and scope_username
                    and scope_session_id
                    and dispatch_pending_opens_visible_turn(message, incoming_text)
                ):
                    turn_started_event = {
                        "type": "agent.turn_started",
                        "ts": utc_ts(),
                        "service_id": service_id,
                        "process_id": process_id,
                        "run_id": message_meta_get(message, "run_id"),
                        "reply_index": reply_index,
                        "scope": {"username": scope_username, "session_id": scope_session_id},
                    }
                    write_jsonl(log_path, turn_started_event)
                    append_scoped_history(
                        {
                            "direction": "event",
                            "ts": utc_ts(),
                            "service_id": service_id,
                            "event_type": "agent.turn_started",
                            "text": f"Agent {service_id} started responding",
                            "event": turn_started_event,
                        },
                        limit=history_limit,
                    )
                    record_session_agent_contact(
                        runtime_root,
                        username=scope_username,
                        session_id=scope_session_id,
                        service_id=service_id,
                        agent_id=resolve_session_agent_id(
                            runtime_root,
                            username=scope_username,
                            session_id=scope_session_id,
                            service_id=service_id,
                        ),
                        provider=str(self_service.get("kind", "")),
                    )

                def emit_provider_event(event: dict[str, Any]) -> None:
                    write_jsonl(
                        log_path,
                        {
                            "type": "service.event",
                            "ts": utc_ts(),
                            "service_id": service_id,
                            "process_id": process_id,
                            "run_id": message_meta_get(message, "run_id"),
                            "event": event,
                        },
                    )
                    if self_service["kind"] == "codex" and event.get("type") == "thread.started":
                        started_session_id = event.get("thread_id")
                        if isinstance(started_session_id, str) and started_session_id.strip():
                            update_process_fields(
                                runtime_root,
                                process_id=process_id,
                                fields={"codex_session_id": started_session_id},
                            )
                            save_codex_session(
                                runtime_root,
                                service_id=service_id,
                                provider_session_id=started_session_id,
                                username=scope_username,
                                session_id=scope_session_id,
                            )
                    # Claude's stream-json --verbose format emits internal conversation
                    # replay events (user, assistant, system) for each tool-use cycle.
                    # These have no user-visible value and flooding history with them
                    # causes history trimming that creates broken empty turn_cluster boxes.
                    # Only forward meaningful service-level events to the UI history.
                    _claude_internal_event_types = {"user", "assistant", "system"}
                    if (
                        self_service["kind"] == "claude"
                        and event.get("type") in _claude_internal_event_types
                    ):
                        return
                    if scope_username and scope_session_id:
                        event_entry = make_history_event_entry(event, service_id=service_id)
                        event_message = build_outgoing_event_message(
                            runtime_root=runtime_root,
                            manifest=manifest,
                            from_node_id=manifest["node_id"],
                            from_service_id=service_id,
                            to_node_id=manifest["node_id"],
                            to_service_id=sender_service_id,
                            process_id=process_id,
                            run_id=message_meta_get(message, "run_id"),
                            entry=event_entry,
                            username=scope_username,
                            session_id=scope_session_id,
                        )
                        send_tx(event_message)

                if self_service["kind"] == "codex":
                    process_record = get_process_record(runtime_root, process_id)
                    scoped_session_id = load_codex_session(
                        runtime_root,
                        service_id=service_id,
                        username=scope_username,
                        session_id=scope_session_id,
                    )
                    if scope_username and scope_session_id:
                        session_id = scoped_session_id
                    else:
                        session_id = scoped_session_id or process_record.get("codex_session_id")

                    final_text, provider_events, next_session_id = run_codex(
                        prompt,
                        session_id=session_id,
                        response_schema_id=self_service.get("response_schema_id"),
                        model=str((self_service.get("config") or {}).get("model") or "").strip() or None,
                        on_event=emit_provider_event,
                    )
                    update_process_fields(
                        runtime_root,
                        process_id=process_id,
                        fields={"codex_session_id": next_session_id},
                    )
                    save_codex_session(
                        runtime_root,
                        service_id=service_id,
                        provider_session_id=next_session_id,
                        username=scope_username,
                        session_id=scope_session_id,
                    )
                elif self_service["kind"] == "claude":
                    scoped_claude_session_id = load_claude_session(
                        runtime_root,
                        service_id=service_id,
                        username=scope_username,
                        session_id=scope_session_id,
                    )
                    final_text, provider_events, next_session_id = run_claude(
                        prompt,
                        session_id=scoped_claude_session_id,
                        response_schema_id=self_service.get("response_schema_id"),
                        on_event=emit_provider_event,
                    )
                    save_claude_session(
                        runtime_root,
                        service_id=service_id,
                        provider_session_id=next_session_id,
                        username=scope_username,
                        session_id=scope_session_id,
                    )
                else:
                    raise RuntimeError(f"unsupported kind: {self_service['kind']}")
        except Exception as exc:
            error_text = repr(exc)
            failure_event = {
                "type": "service.worker_failed",
                "error": error_text,
                "reply_index": reply_index,
                "provider": str(self_service.get("kind", "")),
            }
            write_jsonl(
                log_path,
                {
                    "type": "service.worker_failed",
                    "ts": utc_ts(),
                    "service_id": service_id,
                    "process_id": process_id,
                    "reply_index": reply_index,
                    "scope": {"username": scope_username, "session_id": scope_session_id},
                    "error": error_text,
                },
            )
            emit_turn_completed_event(
                runtime_root=runtime_root,
                manifest=manifest,
                from_service_id=service_id,
                to_service_id=sender_service_id,
                process_id=process_id,
                run_id=message_meta_get(message, "run_id"),
                username=scope_username,
                session_id=scope_session_id,
                send_tx=send_tx,
                reply_index=reply_index,
                status="failed",
                provider=str(self_service.get("kind", "")),
                error=error_text,
            )
            maybe_spawn_failure_recovery(
                username=scope_username,
                session_id=scope_session_id,
                failure_event=failure_event,
                failure_service_id=service_id,
            )
            return

        write_jsonl(
            log_path,
            {
                "type": "message.out",
                "ts": utc_ts(),
                "service_id": service_id,
                "process_id": process_id,
                "reply_index": reply_index,
                "text": final_text,
            },
        )
        try:
            write_jsonl(
                log_path,
                {
                    "type": "service.post_message_out_started",
                    "ts": utc_ts(),
                    "service_id": service_id,
                    "process_id": process_id,
                    "scope": {"username": scope_username, "session_id": scope_session_id},
                },
            )
            visible_text, spawn_requests = parse_service_response(
                final_text,
                self_service.get("response_schema_id"),
            )
            write_jsonl(
                log_path,
                {
                    "type": "service.post_message_out_parsed",
                    "ts": utc_ts(),
                    "service_id": service_id,
                    "process_id": process_id,
                    "scope": {"username": scope_username, "session_id": scope_session_id},
                    "spawn_request_count": len(spawn_requests),
                    "has_visible_text": bool(visible_text),
                },
            )
            for control in spawn_requests:
                spawn_message = make_message(
                    from_node_id=manifest["node_id"],
                    from_service_id=service_id,
                    to_node_id=manifest["node_id"],
                    to_service_id="kernel.spawn",
                    message_type="service.spawn",
                    payload={
                        "service": control["service"],
                        "allowed_peers": control.get("allowed_peers", []),
                    },
                    run_id=message_meta_get(message, "run_id"),
                )
                message_set_meta(spawn_message, "process_id", process_id)
                incoming_auth = message_meta_get(message, "auth")
                if isinstance(incoming_auth, dict):
                    message_set_meta(spawn_message, "auth", dict(incoming_auth))
                send_tx(spawn_message)
                initial_prompt = control.get("initial_prompt")
                if initial_prompt:
                    child_id = control["service"]["service_id"]
                    child_prompt = build_outgoing_message(
                        runtime_root=runtime_root,
                        manifest=manifest,
                        from_node_id=manifest["node_id"],
                        from_service_id=service_id,
                        to_node_id=manifest["node_id"],
                        to_service_id=child_id,
                        process_id=process_id,
                        run_id=message_meta_get(message, "run_id"),
                        text=str(initial_prompt),
                        auth_context=message_meta_get(message, "auth")
                        if isinstance(message_meta_get(message, "auth"), dict)
                        else None,
                    )
                    send_tx(child_prompt)

            if visible_text:
                outgoing = build_outgoing_message(
                    runtime_root=runtime_root,
                    manifest=manifest,
                    from_node_id=manifest["node_id"],
                    from_service_id=service_id,
                    to_node_id=manifest["node_id"],
                    to_service_id=sender_service_id,
                    process_id=process_id,
                    run_id=message_meta_get(message, "run_id"),
                    text=visible_text,
                    username=scope_username,
                    session_id=scope_session_id,
                    auth_context=message_meta_get(message, "auth")
                    if isinstance(message_meta_get(message, "auth"), dict)
                    else None,
                )
                send_tx(outgoing)
            emit_turn_completed_event(
                runtime_root=runtime_root,
                manifest=manifest,
                from_service_id=service_id,
                to_service_id=sender_service_id,
                process_id=process_id,
                run_id=message_meta_get(message, "run_id"),
                username=scope_username,
                session_id=scope_session_id,
                send_tx=send_tx,
                reply_index=reply_index,
                status="success",
                provider=str(self_service.get("kind", "")),
            )

            if scope_username and scope_session_id:
                    try:
                        scope_session_dir = session_dir(
                            runtime_root,
                            username=scope_username,
                            session_id=scope_session_id,
                        )
                        scope_timeline = session_timeline_path(
                            runtime_root,
                            username=scope_username,
                            session_id=scope_session_id,
                        )
                        append_pending_input(
                            runtime_root,
                            username=scope_username,
                            session_id=scope_session_id,
                            entry=make_aize_pending_input(
                                kind="turn_completed",
                                role="system",
                                text="\n".join(
                                    [
                                        "<aize_turn_completed>",
                                        f"  <service_id>{html.escape(service_id)}</service_id>",
                                        f"  <reply_index>{reply_index}</reply_index>",
                                        f"  <process_id>{html.escape(process_id)}</process_id>",
                                        f"  <run_id>{html.escape(str(message_meta_get(message, 'run_id') or ''))}</run_id>",
                                        f"  <completed_at>{html.escape(utc_ts())}</completed_at>",
                                        f"  <session_dir>{html.escape(str(scope_session_dir))}</session_dir>",
                                        f"  <timeline_path>{html.escape(str(scope_timeline))}</timeline_path>",
                                        "  <history_instruction>Read the session files directly for the completed reply and related events instead of relying on inline event text.</history_instruction>",
                                        "</aize_turn_completed>",
                                    ]
                                ),
                            ),
                        )
                        record_session_agent_contact(
                            runtime_root,
                            username=scope_username,
                            session_id=scope_session_id,
                            service_id=service_id,
                            agent_id=resolve_session_agent_id(
                                runtime_root,
                                username=scope_username,
                                session_id=scope_session_id,
                                service_id=service_id,
                            ),
                            provider=str(self_service.get("kind", "")),
                            turn_completed_at=utc_ts(),
                        )
                        write_jsonl(
                            log_path,
                            {
                                "type": "service.post_turn_turn_completed_appended",
                                "ts": utc_ts(),
                                "service_id": service_id,
                                "process_id": process_id,
                                "scope": {"username": scope_username, "session_id": scope_session_id},
                            },
                        )
                        current_audit_state = load_agent_audit_state(
                            runtime_root,
                            service_id=service_id,
                            username=scope_username,
                            session_id=scope_session_id,
                        )
                        if current_audit_state == "panic":
                            save_agent_audit_state(
                                runtime_root,
                                service_id=service_id,
                                username=scope_username,
                                session_id=scope_session_id,
                                audit_state="all_clear",
                            )
                            append_user_history(
                                runtime_root,
                                username=scope_username,
                                session_id=scope_session_id,
                                entry={
                                    "direction": "event",
                                    "ts": utc_ts(),
                                    "service_id": service_id,
                                    "event_type": "service.panic_cleared_after_successful_turn",
                                    "text": "Panic state cleared after a successful worker turn.",
                                    "event": {
                                        "type": "service.panic_cleared_after_successful_turn",
                                        "previous_audit_state": "panic",
                                        "new_audit_state": "all_clear",
                                    },
                                },
                                limit=GOAL_AUDIT_HISTORY_LIMIT,
                            )
                        session_settings = (
                            get_session_settings(runtime_root, username=scope_username, session_id=scope_session_id) or {}
                        )
                        maybe_dispatch_panic_recovery_parent_resume(
                            runtime_root=runtime_root,
                            manifest=manifest,
                            service_id=service_id,
                            process_id=process_id,
                            log_path=log_path,
                            send_tx=send_tx,
                            incoming_text=incoming_text,
                            scope_username=scope_username,
                            scope_session_id=scope_session_id,
                            session_settings=session_settings,
                        )
                        goal_text = str(session_settings.get("goal_text", "")).strip()
                        goal_active = bool(session_settings.get("goal_active", False))
                        goal_completed = bool(session_settings.get("goal_completed", False))
                        goal_progress_state = str(
                            session_settings.get(
                                "goal_progress_state",
                                "complete" if goal_completed else "in_progress",
                            )
                        ).strip().lower()
                        # Audit state is agent-side; load from agent record
                        goal_audit_state = load_agent_audit_state(
                            runtime_root,
                            service_id=service_id,
                            username=scope_username,
                            session_id=scope_session_id,
                        )
                        if (
                            goal_text
                            and goal_active
                            and not goal_completed
                            and goal_progress_state == "in_progress"
                            and goal_audit_state == "all_clear"
                            and bool(session_settings.get("goal_auto_compact_enabled", True))
                        ):
                            auto_compact_threshold = resolve_session_auto_compact_threshold(
                                runtime_root,
                                username=scope_username,
                                session_id=scope_session_id,
                            )
                            if self_service["kind"] == "claude":
                                maybe_auto_compact_claude_session(
                                    runtime_root=runtime_root,
                                    manifest=manifest,
                                    service_id=service_id,
                                    process_id=process_id,
                                    log_path=log_path,
                                    tx_handle=LockedTxHandle(),
                                    sender_service_id=sender_service_id,
                                    run_id=message_meta_get(message, "run_id"),
                                    scope_username=scope_username,
                                    scope_session_id=scope_session_id,
                                    session_id=next_session_id,
                                    threshold_left_percent=auto_compact_threshold,
                                )
                            else:
                                maybe_auto_compact_codex_session(
                                    runtime_root=runtime_root,
                                    manifest=manifest,
                                    service_id=service_id,
                                    process_id=process_id,
                                    log_path=log_path,
                                    tx_handle=LockedTxHandle(),
                                    sender_service_id=sender_service_id,
                                    run_id=message_meta_get(message, "run_id"),
                                    scope_username=scope_username,
                                    scope_session_id=scope_session_id,
                                    session_id=next_session_id,
                                    threshold_left_percent=auto_compact_threshold,
                                )
                            latest_session_settings = (
                                get_session_settings(runtime_root, username=scope_username, session_id=scope_session_id) or {}
                            )
                            latest_context = latest_session_settings.get("last_context_status") or {}
                            context_left_percent = latest_context.get("left_percent")
                            try:
                                if (
                                    context_left_percent is not None
                                    and int(str(context_left_percent)) <= int(auto_compact_threshold)
                                ):
                                    # Store needs_compact at agent level; also shadow to session for backward compat
                                    save_agent_audit_state(
                                        runtime_root,
                                        service_id=service_id,
                                        username=scope_username,
                                        session_id=scope_session_id,
                                        audit_state="needs_compact",
                                    )
                                    update_session_goal_flags(
                                        runtime_root,
                                        username=scope_username,
                                        session_id=scope_session_id,
                                        goal_completed=False,
                                        goal_progress_state="in_progress",
                                    )
                            except (TypeError, ValueError):
                                pass
                            goal_audit_state = load_agent_audit_state(
                                runtime_root,
                                service_id=service_id,
                                username=scope_username,
                                session_id=scope_session_id,
                            )
                        # The just-finished turn always appends a fresh turn_completed marker,
                        # so the post-turn state machine should evaluate against that marker
                        # instead of only checking whether the incoming batch already contained one.
                        turn_completed_input_present = True
                        goal_input_present = (
                            batch_has_input_kind(incoming_text, "goal_update")
                            or batch_has_input_kind(incoming_text, "goal_feedback")
                            or batch_has_input_kind(incoming_text, "restart_resume")
                        )
                        goal_should_continue = bool(
                            turn_completed_input_present
                            and goal_text
                            and goal_active
                            and not goal_completed
                            and goal_progress_state == "in_progress"
                            and goal_audit_state in {"all_clear", "needs_compact"}
                        )
                        write_jsonl(
                            log_path,
                            {
                                "type": "service.post_turn_goal_state",
                                "ts": utc_ts(),
                                "service_id": service_id,
                                "process_id": process_id,
                                "scope": {"username": scope_username, "session_id": scope_session_id},
                                "goal_active": goal_active,
                                "goal_completed": goal_completed,
                                "goal_progress_state": goal_progress_state,
                                "goal_audit_state": goal_audit_state,
                                "turn_completed_input_present": turn_completed_input_present,
                                "goal_input_present": goal_input_present,
                                "goal_should_continue": goal_should_continue,
                            },
                        )
                        if goal_should_continue:
                            gm_queue = append_goal_manager_pending_input(
                                runtime_root,
                                username=scope_username,
                                session_id=scope_session_id,
                                entry={
                                    "kind": "turn_completed",
                                    "ts": utc_ts(),
                                    "service_id": service_id,
                                    "goal_id": str(
                                        session_settings.get("active_goal_id")
                                        or session_settings.get("goal_id")
                                        or ""
                                    ).strip(),
                                },
                            )
                            write_jsonl(
                                log_path,
                                {
                                    "type": "service.post_turn_followup_started",
                                    "ts": utc_ts(),
                                    "service_id": service_id,
                                    "process_id": process_id,
                                    "scope": {"username": scope_username, "session_id": scope_session_id},
                                    "goal_manager_pending_count": len(gm_queue),
                                },
                            )
                            def run_goal_manager() -> None:
                                try:
                                    gm_work_items = drain_goal_manager_pending_inputs(
                                        runtime_root,
                                        username=scope_username,
                                        session_id=scope_session_id,
                                    )
                                    if not gm_work_items:
                                        return
                                    goal_audit_job_id = f"goal-audit-{uuid.uuid4().hex[:8]}"
                                    goal_id = str(
                                        session_settings.get("active_goal_id")
                                        or session_settings.get("goal_id")
                                        or ""
                                    ).strip()
                                    def goal_history_sink(entry: dict[str, Any]) -> None:
                                        append_scoped_history(entry, limit=GOAL_AUDIT_HISTORY_LIMIT)
                                    def goal_provider_event_sink(event: dict[str, Any]) -> None:
                                        provider_event_type = str(event.get("type") or "event").strip() or "event"
                                        history_entry = make_history_event_entry(event, service_id=service_id)
                                        history_entry["direction"] = "agent"
                                        history_entry["from"] = service_id
                                        history_entry["session_id"] = scope_session_id
                                        history_entry["event_type"] = f"service.goal_audit_provider_event.{provider_event_type}"
                                        history_entry["event"] = {
                                            "type": "service.goal_audit_provider_event",
                                            "goal_audit_job_id": goal_audit_job_id,
                                            "provider_event": event,
                                        }
                                        write_jsonl(
                                            log_path,
                                            {
                                                "type": "service.goal_audit_provider_event",
                                                "ts": utc_ts(),
                                                "service_id": service_id,
                                                "process_id": process_id,
                                                "goal_audit_job_id": goal_audit_job_id,
                                                "scope": {"username": scope_username, "session_id": scope_session_id},
                                                "provider_event": event,
                                            },
                                        )
                                        goal_history_sink(history_entry)
                                    started_event = {
                                        "type": "service.goal_audit_started",
                                        "ts": utc_ts(),
                                        "service_id": service_id,
                                        "process_id": process_id,
                                        "goal_audit_job_id": goal_audit_job_id,
                                        "scope": {"username": scope_username, "session_id": scope_session_id},
                                        "goal_id": goal_id,
                                        "goal_text": goal_text,
                                        "goal_manager_work_items": gm_work_items,
                                    }
                                    goal_manager_state_path = session_goal_manager_state_path(
                                        runtime_root,
                                        username=scope_username,
                                        session_id=scope_session_id,
                                    )
                                    goal_manager_state = read_json_file(goal_manager_state_path) or {}
                                    goal_manager_state.update(
                                        {
                                            "state": "running",
                                            "service_id": service_id,
                                            "goal_audit_job_id": goal_audit_job_id,
                                            "goal_id": goal_id,
                                            "goal_text": goal_text,
                                            "pending_work_items": gm_work_items,
                                            "updated_at": utc_ts(),
                                        }
                                    )
                                    write_json_file(goal_manager_state_path, goal_manager_state)
                                    write_jsonl(log_path, started_event)
                                    goal_history_sink(
                                        {
                                            "direction": "agent",
                                            "ts": utc_ts(),
                                            "from": service_id,
                                            "session_id": scope_session_id,
                                            "event_type": "service.goal_audit_started",
                                            "text": "GoalManager is reviewing this session.",
                                            "event": started_event,
                                        }
                                    )
                                    # Audit state is agent-side: read from agent record
                                    resolved_audit_state = load_agent_audit_state(
                                        runtime_root,
                                        service_id=service_id,
                                        username=scope_username,
                                        session_id=scope_session_id,
                                    )
                                    audit: dict[str, Any] | None = None
                                    compact_event: dict[str, Any] | None = None
                                    if resolved_audit_state == "all_clear":
                                        snapshot = get_user_history(
                                            runtime_root,
                                            username=scope_username,
                                            session_id=scope_session_id,
                                        )
                                        audit = run_goal_audit(
                                            runtime_root=runtime_root,
                                            username=scope_username,
                                            session_id=scope_session_id,
                                            goal_text=goal_text,
                                            history_entries=snapshot,
                                            provider_kind=str(self_service.get("kind", "codex")),
                                            on_event=goal_provider_event_sink,
                                        )
                                        persist_goal_audit_completion(
                                            runtime_root=runtime_root,
                                            log_path=log_path,
                                            service_id=service_id,
                                            process_id=process_id,
                                            goal_audit_job_id=goal_audit_job_id,
                                            username=scope_username,
                                            session_id=scope_session_id,
                                            audit={**audit, "goal_id": goal_id, "goal_text": goal_text},
                                            history_sink=goal_history_sink,
                                        )
                                        compact_event = handle_goal_manager_compact_request(
                                            runtime_root=runtime_root,
                                            repo_root=Path(__file__).resolve().parents[2],
                                            log_path=log_path,
                                            service_id=service_id,
                                            process_id=process_id,
                                            goal_audit_job_id=goal_audit_job_id,
                                            username=scope_username,
                                            session_id=scope_session_id,
                                            audit=audit,
                                            history_sink=goal_history_sink,
                                        )
                                        resolved_audit_state = str(audit["audit_state"]).strip().lower()
                                    elif resolved_audit_state == "needs_compact":
                                        compact_event = handle_goal_manager_compact_request(
                                            runtime_root=runtime_root,
                                            repo_root=Path(__file__).resolve().parents[2],
                                            log_path=log_path,
                                            service_id=service_id,
                                            process_id=process_id,
                                            goal_audit_job_id=goal_audit_job_id,
                                            username=scope_username,
                                            session_id=scope_session_id,
                                            audit={
                                                "request_compact": True,
                                                "request_compact_reason": "TurnCompleted auto-compact threshold exceeded.",
                                            },
                                            history_sink=goal_history_sink,
                                        )
                                    compact_recovered = False
                                    if resolved_audit_state == "needs_compact":
                                        if compact_event is None:
                                            resolved_audit_state = "panic"
                                        elif str(compact_event.get("type")) == "service.goal_manager_compact_failed":
                                            resolved_audit_state = "panic"
                                        elif str(compact_event.get("compaction")) == "suppressed_by_session_setting":
                                            resolved_audit_state = "needs_compact"
                                        else:
                                            resolved_audit_state = "all_clear"
                                            compact_recovered = True
                                    audit_progress_state = (
                                        str(audit["progress_state"]).strip().lower()
                                        if audit is not None
                                        else "in_progress"
                                    )
                                    if audit is not None:
                                        reviewed_turns = audit.get("pending_turn_completed_events", [])
                                        if isinstance(reviewed_turns, list):
                                            reviewed_ts = [
                                                str(item.get("completed_at") or "").strip()
                                                for item in reviewed_turns
                                                if isinstance(item, dict) and str(item.get("completed_at") or "").strip()
                                            ]
                                            if reviewed_ts:
                                                update_goal_manager_review_cursor(
                                                    runtime_root,
                                                    username=scope_username,
                                                    session_id=scope_session_id,
                                                    last_turn_completed_at=max(reviewed_ts),
                                                )
                                    if audit_progress_state == "complete":
                                        parent_session_id = str(
                                            session_settings.get("parent_session_id") or ""
                                        ).strip()
                                        if parent_session_id and parent_session_id != scope_session_id:
                                            parent_progress = complete_session_child(
                                                runtime_root,
                                                username=scope_username,
                                                parent_session_id=parent_session_id,
                                                child_session_id=scope_session_id,
                                            )
                                            if parent_progress is not None:
                                                parent_session = get_session_settings(
                                                    runtime_root,
                                                    username=scope_username,
                                                    session_id=parent_session_id,
                                                ) or {}
                                                parent_service_id = str(
                                                    parent_session.get("service_id") or service_id
                                                ).strip()
                                                parent_summary = (
                                                    f"Child session {scope_session_id} completed goal work."
                                                )
                                                if not bool(parent_progress.get("waiting_on_children")):
                                                    parent_goal_id = str(
                                                        parent_session.get("active_goal_id")
                                                        or parent_session.get("goal_id")
                                                        or ""
                                                    ).strip()
                                                    append_pending_input(
                                                        runtime_root,
                                                        username=scope_username,
                                                        session_id=parent_session_id,
                                                        entry=make_aize_pending_input(
                                                            kind="goal_feedback",
                                                            role="system",
                                                            text=default_goal_continue_xml(summary=parent_summary),
                                                        ),
                                                    )
                                                    append_goal_manager_pending_input(
                                                        runtime_root,
                                                        username=scope_username,
                                                        session_id=parent_session_id,
                                                        entry={
                                                            "kind": "child_session_completed",
                                                            "ts": utc_ts(),
                                                            "service_id": service_id,
                                                            "child_session_id": scope_session_id,
                                                            "goal_id": parent_goal_id,
                                                        },
                                                    )
                                                    send_tx(
                                                        make_dispatch_pending_message(
                                                            manifest=manifest,
                                                            from_service_id=service_id,
                                                            to_service_id=parent_service_id,
                                                            process_id=process_id,
                                                            run_id=f"child-complete-{uuid.uuid4().hex[:8]}",
                                                            username=scope_username,
                                                            session_id=parent_session_id,
                                                            auth_context=None,
                                                            reason="child_session_completed",
                                                            session_agent_id=resolve_session_agent_id(
                                                                runtime_root,
                                                                username=scope_username,
                                                                session_id=parent_session_id,
                                                                service_id=parent_service_id,
                                                            ),
                                                        )
                                                    )
                                        save_agent_audit_state(
                                            runtime_root,
                                            service_id=service_id,
                                            username=scope_username,
                                            session_id=scope_session_id,
                                            audit_state=resolved_audit_state,
                                        )
                                        completion_ok, completion_reason = _parent_resume_validated_for_recovery_completion(
                                            runtime_root=runtime_root,
                                            username=scope_username,
                                            recovery_session_settings=session_settings,
                                        )
                                        if not completion_ok:
                                            update_session_goal_flags(
                                                runtime_root,
                                                username=scope_username,
                                                session_id=scope_session_id,
                                                goal_id=goal_id,
                                                goal_completed=False,
                                                goal_progress_state="in_progress",
                                            )
                                            scope_session_dir = session_dir(
                                                runtime_root,
                                                username=scope_username,
                                                session_id=scope_session_id,
                                            )
                                            scope_timeline = session_timeline_path(
                                                runtime_root,
                                                username=scope_username,
                                                session_id=scope_session_id,
                                            )
                                            append_pending_input(
                                                runtime_root,
                                                username=scope_username,
                                                session_id=scope_session_id,
                                                entry=make_aize_pending_input(
                                                    kind="completion_override",
                                                    role="system",
                                                    text=_session_completion_override_xml(
                                                        reason=completion_reason,
                                                        session_dir_path=str(scope_session_dir),
                                                        timeline_path=str(scope_timeline),
                                                    ),
                                                ),
                                            )
                                            append_user_history(
                                                runtime_root,
                                                username=scope_username,
                                                session_id=scope_session_id,
                                                entry={
                                                    "direction": "event",
                                                    "ts": utc_ts(),
                                                    "service_id": service_id,
                                                    "event_type": "service.session_completion_overridden",
                                                    "text": f"Session completion overridden: {completion_reason}",
                                                    "event": {
                                                        "type": "service.session_completion_overridden",
                                                        "reason": completion_reason,
                                                    },
                                                },
                                                limit=GOAL_AUDIT_HISTORY_LIMIT,
                                            )
                                            send_tx(
                                                make_dispatch_pending_message(
                                                    manifest=manifest,
                                                    from_service_id=service_id,
                                                    to_service_id=service_id,
                                                    process_id=process_id,
                                                    run_id=f"completion-override-{uuid.uuid4().hex[:8]}",
                                                    username=scope_username,
                                                    session_id=scope_session_id,
                                                    auth_context=None,
                                                    reason="session_completion_overridden",
                                                    session_agent_id=resolve_session_agent_id(
                                                        runtime_root,
                                                        username=scope_username,
                                                        session_id=scope_session_id,
                                                        service_id=service_id,
                                                    ),
                                                )
                                            )
                                            return
                                        update_session_goal_flags(
                                            runtime_root,
                                            username=scope_username,
                                            session_id=scope_session_id,
                                            goal_id=goal_id,
                                            goal_completed=True,
                                            goal_progress_state=audit_progress_state,
                                        )
                                        latest_session_settings = get_session_settings(
                                            runtime_root,
                                            username=scope_username,
                                            session_id=scope_session_id,
                                        )
                                        maybe_release_session_provider(
                                            runtime_root,
                                            username=scope_username,
                                            session_id=scope_session_id,
                                            talk=latest_session_settings,
                                        )
                                        return
                                    raw_child_goal_requests = (
                                        list(audit.get("child_goal_requests", []))
                                        if audit is not None and isinstance(audit.get("child_goal_requests"), list)
                                        else []
                                    )
                                    for child_request in raw_child_goal_requests:
                                        if not isinstance(child_request, dict):
                                            continue
                                        child_service_id = str(child_request.get("service_id") or "").strip()
                                        child_goal_text = str(child_request.get("goal_text") or "").strip()
                                        child_label = str(child_request.get("label") or "").strip()
                                        if not child_service_id or not child_goal_text:
                                            continue
                                        child_session = create_child_conversation_session(
                                            runtime_root,
                                            username=scope_username,
                                            parent_session_id=scope_session_id,
                                            label=child_label or "Subgoal",
                                            goal_text=child_goal_text,
                                        )
                                        if child_session is None:
                                            continue
                                        child_session_id = str(child_session.get("session_id") or "").strip()
                                        child_goal_id = str(
                                            child_session.get("active_goal_id")
                                            or child_session.get("goal_id")
                                            or ""
                                        ).strip()
                                        append_pending_input(
                                            runtime_root,
                                            username=scope_username,
                                            session_id=child_session_id,
                                            entry=make_aize_pending_input(
                                                kind="goal_update",
                                                role="system",
                                                text=_goal_update_xml(
                                                    goal_id=child_goal_id,
                                                    goal_text=child_goal_text,
                                                ),
                                            ),
                                        )
                                        record_session_agent_contact(
                                            runtime_root,
                                            username=scope_username,
                                            session_id=child_session_id,
                                            service_id=child_service_id,
                                        )
                                        write_jsonl(
                                            log_path,
                                            {
                                                "type": "service.goal_child_session_created",
                                                "ts": utc_ts(),
                                                "service_id": service_id,
                                                "process_id": process_id,
                                                "scope": {"username": scope_username, "session_id": scope_session_id},
                                                "child_session_id": child_session_id,
                                                "dispatch_service_id": child_service_id,
                                                "goal_text": child_goal_text,
                                            },
                                        )
                                        goal_history_sink(
                                            {
                                                "direction": "agent",
                                                "ts": utc_ts(),
                                                "from": service_id,
                                                "session_id": scope_session_id,
                                                "event_type": "service.goal_child_session_created",
                                                "text": f"GoalManager created child session {child_session_id}.",
                                                "event": {
                                                    "type": "service.goal_child_session_created",
                                                    "child_session_id": child_session_id,
                                                    "dispatch_service_id": child_service_id,
                                                    "goal_text": child_goal_text,
                                                },
                                            }
                                        )
                                        send_tx(
                                            make_dispatch_pending_message(
                                                manifest=manifest,
                                                from_service_id=service_id,
                                                to_service_id=child_service_id,
                                                process_id=process_id,
                                                run_id=f"child-goal-{uuid.uuid4().hex[:8]}",
                                                username=scope_username,
                                                session_id=child_session_id,
                                                auth_context=None,
                                                reason="child_goal_request",
                                                session_agent_id=resolve_session_agent_id(
                                                    runtime_root,
                                                    username=scope_username,
                                                    session_id=child_session_id,
                                                    service_id=child_service_id,
                                                ),
                                            )
                                        )
                                    save_agent_audit_state(
                                        runtime_root,
                                        service_id=service_id,
                                        username=scope_username,
                                        session_id=scope_session_id,
                                        audit_state=resolved_audit_state,
                                    )
                                    update_session_goal_flags(
                                        runtime_root,
                                        username=scope_username,
                                        session_id=scope_session_id,
                                        goal_id=goal_id,
                                        goal_completed=False,
                                        goal_progress_state=audit_progress_state,
                                    )
                                    latest_session_settings = get_session_settings(
                                        runtime_root,
                                        username=scope_username,
                                        session_id=scope_session_id,
                                    ) or {}
                                    if resolved_audit_state == "panic" and compact_event is not None:
                                        spawn_panic_recovery(
                                            username=scope_username,
                                            session_id=scope_session_id,
                                            panic_event=compact_event,
                                            panic_service_id=service_id,
                                        )
                                    agent_welcome_enabled = bool(
                                        latest_session_settings.get("agent_welcome_enabled", False)
                                    )
                                    latest_goal_id = str(
                                        latest_session_settings.get("active_goal_id")
                                        or latest_session_settings.get("goal_id")
                                        or ""
                                    ).strip()
                                    if goal_id and latest_goal_id and latest_goal_id != goal_id:
                                        return
                                    if not goal_audit_should_enqueue_agent_followup(
                                        progress_state=audit_progress_state,
                                        audit_state=resolved_audit_state,
                                    ):
                                        return
                                    feedback_xml = (
                                        (audit["continue_xml"] or default_goal_continue_xml(summary=audit["summary"]))
                                        if audit is not None
                                        else default_goal_continue_xml(
                                            summary="Context compact succeeded. Continue toward the active goal."
                                        )
                                    )
                                    if compact_recovered:
                                        feedback_xml = default_goal_continue_xml(
                                            summary=(
                                                (
                                                    f"{audit['summary']}\n\n"
                                                    if audit is not None and audit["summary"]
                                                    else ""
                                                )
                                                + "Context compact succeeded. Continue toward the active goal."
                                            )
                                        )
                                    raw_agent_directives = (
                                        list(audit.get("agent_directives", []))
                                        if audit is not None and isinstance(audit.get("agent_directives"), list)
                                        else []
                                    )
                                    normalized_directives: list[dict[str, Any]] = []
                                    if raw_agent_directives:
                                        for directive in raw_agent_directives:
                                            if not isinstance(directive, dict):
                                                continue
                                            directive_service_id = str(directive.get("service_id") or "").strip()
                                            if not directive_service_id:
                                                continue
                                            normalized_directives.append(
                                                {
                                                    "service_id": directive_service_id,
                                                    "audit_state": str(
                                                        directive.get("audit_state") or "all_clear"
                                                    ).strip().lower(),
                                                    "continue_xml": str(
                                                        directive.get("continue_xml") or ""
                                                    ).strip(),
                                                    "request_compact": bool(
                                                        directive.get("request_compact", False)
                                                    ),
                                                    "request_compact_reason": str(
                                                        directive.get("request_compact_reason") or ""
                                                    ).strip(),
                                                }
                                            )
                                    if not normalized_directives:
                                        normalized_directives = [
                                            {
                                                "service_id": service_id,
                                                "audit_state": resolved_audit_state,
                                                "continue_xml": feedback_xml,
                                                "request_compact": resolved_audit_state == "needs_compact",
                                                "request_compact_reason": (
                                                    str(audit.get("request_compact_reason") or "").strip()
                                                    if audit is not None
                                                    else ""
                                                ),
                                            }
                                        ]
                                    elif not agent_welcome_enabled:
                                        normalized_directives = [
                                            directive
                                            for directive in normalized_directives
                                            if str(directive.get("service_id") or "").strip() == service_id
                                        ] or [
                                            {
                                                "service_id": service_id,
                                                "audit_state": resolved_audit_state,
                                                "continue_xml": feedback_xml,
                                                "request_compact": resolved_audit_state == "needs_compact",
                                                "request_compact_reason": (
                                                    str(audit.get("request_compact_reason") or "").strip()
                                                    if audit is not None
                                                    else ""
                                                ),
                                            }
                                        ]
                                    for directive in normalized_directives:
                                        directive_state = str(directive.get("audit_state") or "all_clear").strip().lower()
                                        if directive_state not in {"all_clear", "needs_compact", "panic"}:
                                            directive_state = "all_clear"
                                        save_agent_audit_state(
                                            runtime_root,
                                            service_id=str(directive["service_id"]),
                                            username=scope_username,
                                            session_id=scope_session_id,
                                            audit_state=directive_state,
                                        )
                                    contacted_agents = list_session_agent_contacts(
                                        runtime_root,
                                        username=scope_username,
                                        session_id=scope_session_id,
                                    )
                                    dispatch_targets = goal_followup_dispatch_targets(
                                        contacted_agents if agent_welcome_enabled else [],
                                        normalized_directives,
                                    )
                                    queued_targeted_followup = False
                                    for directive in normalized_directives:
                                        directive_service_id = str(directive.get("service_id") or "").strip()
                                        directive_state = str(directive.get("audit_state") or "all_clear").strip().lower()
                                        directive_feedback_xml = str(directive.get("continue_xml") or "").strip()
                                        if (
                                            directive_service_id
                                            and directive_state == "all_clear"
                                            and directive_feedback_xml
                                        ):
                                            append_service_pending_input(
                                                runtime_root,
                                                service_id=directive_service_id,
                                                agent_id=resolve_session_agent_id(
                                                    runtime_root,
                                                    username=scope_username,
                                                    session_id=scope_session_id,
                                                    service_id=directive_service_id,
                                                ),
                                                username=scope_username,
                                                session_id=scope_session_id,
                                                entry=make_aize_pending_input(
                                                    kind="goal_feedback",
                                                    role="system",
                                                    text=directive_feedback_xml,
                                                ),
                                            )
                                            queued_targeted_followup = True
                                            _feedback_summary = str(audit.get("summary") or "").strip() if audit is not None else ""
                                            append_scoped_history(
                                                {
                                                    "direction": "session_input",
                                                    "kind": "goal_feedback",
                                                    "ts": utc_ts(),
                                                    "service_id": directive_service_id,
                                                    "to": directive_service_id,
                                                    "text": _feedback_summary or "GoalManager requested more work",
                                                },
                                                limit=GOAL_AUDIT_HISTORY_LIMIT,
                                            )
                                    if not queued_targeted_followup:
                                        append_pending_input(
                                            runtime_root,
                                            username=scope_username,
                                            session_id=scope_session_id,
                                            entry=make_aize_pending_input(
                                                kind="goal_feedback",
                                                role="system",
                                                text=feedback_xml,
                                            ),
                                        )
                                        dispatch_targets = [service_id]
                                    for dispatch_service_id in dispatch_targets:
                                        pending_for_target = load_service_pending_inputs(
                                            runtime_root,
                                            service_id=dispatch_service_id,
                                            agent_id=resolve_session_agent_id(
                                                runtime_root,
                                                username=scope_username,
                                                session_id=scope_session_id,
                                                service_id=dispatch_service_id,
                                            ),
                                            username=scope_username,
                                            session_id=scope_session_id,
                                        )
                                        write_jsonl(
                                            log_path,
                                            {
                                                "type": "service.goal_audit_dispatch_check",
                                                "ts": utc_ts(),
                                                "service_id": service_id,
                                                "process_id": process_id,
                                                "scope": {"username": scope_username, "session_id": scope_session_id},
                                                "dispatch_target": dispatch_service_id,
                                                "pending_for_target_count": len(pending_for_target),
                                                "queued_targeted_followup": queued_targeted_followup,
                                                "will_skip": bool(not pending_for_target and queued_targeted_followup),
                                            },
                                        )
                                        if not pending_for_target and queued_targeted_followup:
                                            continue
                                        goal_message = make_dispatch_pending_message(
                                            manifest=manifest,
                                            from_service_id=service_id,
                                            to_service_id=dispatch_service_id,
                                            process_id=process_id,
                                            run_id=f"goal-audit-{uuid.uuid4().hex[:8]}",
                                            username=scope_username,
                                            session_id=scope_session_id,
                                            auth_context=None,
                                            reason="goal_feedback",
                                            session_agent_id=resolve_session_agent_id(
                                                runtime_root,
                                                username=scope_username,
                                                session_id=scope_session_id,
                                                service_id=dispatch_service_id,
                                            ),
                                        )
                                        send_tx(goal_message)
                                except Exception as exc:
                                    save_agent_audit_state(
                                        runtime_root,
                                        service_id=service_id,
                                        username=scope_username,
                                        session_id=scope_session_id,
                                        audit_state="panic",
                                    )
                                    update_session_goal_flags(
                                        runtime_root,
                                        username=scope_username,
                                        session_id=scope_session_id,
                                        goal_completed=False,
                                        goal_progress_state="in_progress",
                                    )
                                    write_jsonl(
                                        log_path,
                                        {
                                            "type": "service.goal_audit_failed",
                                            "ts": utc_ts(),
                                            "service_id": service_id,
                                            "process_id": process_id,
                                            "goal_audit_job_id": goal_audit_job_id,
                                            "scope": {"username": scope_username, "session_id": scope_session_id},
                                            "error": repr(exc),
                                        },
                                    )
                                    goal_history_sink(
                                        {
                                            "direction": "agent",
                                            "ts": utc_ts(),
                                            "from": service_id,
                                            "session_id": scope_session_id,
                                            "event_type": "service.goal_audit_failed",
                                            "text": f"GoalManager audit failed: {exc!r}",
                                            "event": {
                                                "type": "service.goal_audit_failed",
                                                "goal_audit_job_id": goal_audit_job_id,
                                                "error": repr(exc),
                                            },
                                        }
                                    )
                                    maybe_spawn_failure_recovery(
                                        username=scope_username,
                                        session_id=scope_session_id,
                                        failure_event={
                                            "type": "service.goal_audit_failed",
                                            "error": repr(exc),
                                            "service_id": service_id,
                                            "process_id": process_id,
                                            "goal_audit_job_id": goal_audit_job_id,
                                        },
                                        failure_service_id=service_id,
                                    )

                            run_goal_manager()

                        latest_post_followup_settings = (
                            get_session_settings(runtime_root, username=scope_username, session_id=scope_session_id) or {}
                        )
                        latest_goal_text = str(latest_post_followup_settings.get("goal_text", "")).strip()
                        latest_goal_active = bool(latest_post_followup_settings.get("goal_active", False))
                        latest_goal_completed = bool(latest_post_followup_settings.get("goal_completed", False))
                        latest_goal_progress_state = str(
                            latest_post_followup_settings.get(
                                "goal_progress_state",
                                "complete" if latest_goal_completed else "in_progress",
                            )
                        ).strip().lower()
                        # Audit state is agent-side for followup decision
                        latest_goal_audit_state = load_agent_audit_state(
                            runtime_root,
                            service_id=service_id,
                            username=scope_username,
                            session_id=scope_session_id,
                        )
                        may_auto_followup = bool(
                            latest_goal_text
                            and latest_goal_active
                            and not latest_goal_completed
                            and latest_goal_progress_state == "in_progress"
                            and latest_goal_audit_state == "all_clear"
                        )
                        next_pending_inputs = load_pending_inputs(
                            runtime_root,
                            username=scope_username,
                            session_id=scope_session_id,
                        )
                        has_actionable_pending = any(
                            str(item.get("kind", "")) != "turn_completed" for item in next_pending_inputs
                        )
                        write_jsonl(
                            log_path,
                            {
                                "type": "service.post_turn_followup_decision",
                                "ts": utc_ts(),
                                "service_id": service_id,
                                "process_id": process_id,
                                "scope": {"username": scope_username, "session_id": scope_session_id},
                                "goal_active": latest_goal_active,
                                "goal_completed": latest_goal_completed,
                                "goal_progress_state": latest_goal_progress_state,
                                "goal_audit_state": latest_goal_audit_state,
                                "may_auto_followup": may_auto_followup,
                                "has_actionable_pending": has_actionable_pending,
                                "auto_goal_update_injected": False,
                            },
                        )
                        if has_actionable_pending and may_auto_followup:
                            send_tx(
                                make_dispatch_pending_message(
                                    manifest=manifest,
                                    from_service_id="service-http-001",
                                    to_service_id=service_id,
                                    process_id=process_id,
                                    run_id=f"turn-complete-{uuid.uuid4().hex[:8]}",
                                    username=scope_username,
                                    session_id=scope_session_id,
                                    auth_context=message_meta_get(message, "auth")
                                    if isinstance(message_meta_get(message, "auth"), dict)
                                    else None,
                                    reason="turn_completed",
                                    session_agent_id=resolve_session_agent_id(
                                        runtime_root,
                                        username=scope_username,
                                        session_id=scope_session_id,
                                        service_id=service_id,
                                    ),
                                )
                            )
                        elif has_actionable_pending and not may_auto_followup:
                            write_jsonl(
                                log_path,
                                {
                                    "type": "service.post_turn_followup_suppressed",
                                    "ts": utc_ts(),
                                    "service_id": service_id,
                                    "process_id": process_id,
                                    "scope": {"username": scope_username, "session_id": scope_session_id},
                                    "reason": "state_disallows_followup",
                                    "goal_audit_state": latest_goal_audit_state,
                                },
                            )
                    except Exception as exc:
                        failure_event = {
                            "type": "service.post_turn_followup_failed",
                            "error": repr(exc),
                            "service_id": service_id,
                            "process_id": process_id,
                        }
                        write_jsonl(
                            log_path,
                            {
                                "type": "service.post_turn_followup_failed",
                                "ts": utc_ts(),
                                "service_id": service_id,
                                "process_id": process_id,
                                "scope": {"username": scope_username, "session_id": scope_session_id},
                                "error": repr(exc),
                            },
                        )
                        maybe_spawn_failure_recovery(
                            username=scope_username,
                            session_id=scope_session_id,
                            failure_event=failure_event,
                            failure_service_id=service_id,
                        )
        except Exception as exc:
            error_text = repr(exc)
            failure_event = {
                "type": "service.post_message_out_failed",
                "error": error_text,
                "reply_index": reply_index,
                "provider": str(self_service.get("kind", "")),
                "response_excerpt": final_text[:800] if isinstance(final_text, str) else "",
            }
            write_jsonl(
                log_path,
                {
                    "type": "service.post_message_out_failed",
                    "ts": utc_ts(),
                    "service_id": service_id,
                    "process_id": process_id,
                    "scope": {"username": scope_username, "session_id": scope_session_id},
                    "error": error_text,
                },
            )
            emit_turn_completed_event(
                runtime_root=runtime_root,
                manifest=manifest,
                from_service_id=service_id,
                to_service_id=sender_service_id,
                process_id=process_id,
                run_id=message_meta_get(message, "run_id"),
                username=scope_username,
                session_id=scope_session_id,
                send_tx=send_tx,
                reply_index=reply_index,
                status="failed",
                provider=str(self_service.get("kind", "")),
                error=error_text,
            )
            maybe_spawn_failure_recovery(
                username=scope_username,
                session_id=scope_session_id,
                failure_event=failure_event,
                failure_service_id=service_id,
            )
        if reply_index >= int(self_service["max_turns"]) and not done_sent.is_set():
            done_sent.set()
            done_message = make_message(
                from_node_id=manifest["node_id"],
                from_service_id=service_id,
                to_node_id=manifest["node_id"],
                to_service_id="kernel.control",
                message_type="service.done",
                payload={"service_id": service_id, "process_id": process_id},
                run_id=manifest["run_id"],
            )
            message_set_meta(done_message, "process_id", process_id)
            send_tx(done_message)
            write_jsonl(
                log_path,
                {
                    "type": "service_adapter.stopped",
                    "ts": utc_ts(),
                    "service_id": service_id,
                    "process_id": process_id,
                    "reason": "max_turns_reached",
                },
            )

    with router_conn:
        for raw in router_conn:
            line = raw.strip()
            if not line:
                continue
            message = decode_line(line)
            if message.get("type") not in {"prompt", "dispatch_pending"}:
                continue

            with reply_count_lock:
                reply_count += 1
                current_reply_index = reply_count

            if self_service["kind"] == "codex":
                worker = threading.Thread(
                    target=process_prompt_message,
                    args=(message,),
                    kwargs={"reply_index": current_reply_index},
                    daemon=True,
                )
                workers.append(worker)
                worker.start()
            else:
                process_prompt_message(message, reply_index=current_reply_index)

            if current_reply_index >= int(self_service["max_turns"]):
                break

        for worker in list(workers):
            worker.join()

    if done_sent.is_set():
        update_service_process(
            runtime_root,
            service_id=service_id,
            process_id=process_id,
            status="stopped",
        )
        register_process(
            runtime_root,
            process_id=process_id,
            service_id=service_id,
            node_id=manifest["node_id"],
            status="stopped",
            reason="max_turns_reached",
        )
    return 0
