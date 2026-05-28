from __future__ import annotations

import html
import json
import threading
import uuid
import re
from pathlib import Path
from typing import Any, Callable

from kernel.auth import GOAL_MANAGER_USERNAME, auth_context_allows
from kernel.lifecycle import get_process_record, register_process, update_process_fields
from kernel.registry import add_allowed_peer, list_service_records, update_service_process
from runtime.compaction import (
    GOAL_AUDIT_HISTORY_LIMIT,
    maybe_auto_compact_claude_session,
    maybe_auto_compact_codex_session,
    maybe_auto_compact_gemini_session,
    resolve_session_auto_compact_threshold,
    wait_for_service_record,
)
from runtime.communication_goal import (
    is_continuous_communication_session,
    should_complete_communication_goal_after_reply as _should_complete_communication_goal_after_reply,
    should_preserve_prompt_cycle_progress_during_goal_review as _should_preserve_prompt_cycle_progress_during_goal_review,
)
from runtime.dispatch_policy import (
    DEFAULT_PROVIDER_SESSION_SLOT,
    dispatch_provider_session_slot as _dispatch_provider_session_slot,
    dispatch_reason as _dispatch_reason,
    dispatch_reason_uses_service_pending_only as _dispatch_reason_uses_service_pending_only,
    interactive_worker_resume_target as _interactive_worker_resume_target,
    normalize_provider_session_slot as _normalize_provider_session_slot,
    slot_agent_id as _slot_agent_id,
)
from runtime.dispatch_queue import dispatch_priority
from runtime.dispatch_state import (
    dispatch_target_agent_id as _dispatch_target_agent_id,
    post_turn_followup_pending_state as _post_turn_followup_pending_state,
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
    persist_goal_audit_failure,
)
from runtime.panic_recovery import (
    ensure_panic_recovery_session,
    panic_recovery_bootstrap_xml,
)
from runtime.session_lifecycle import enqueue_goal_manager_lifecycle_review
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
from runtime.persistent_state_pkg import (
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
    active_agent_profile_priority,
    active_goal_manager_priority,
    join_session_agent,
    lease_session_service,
    load_agent_audit_state,
    load_codex_session,
    load_claude_session,
    load_gemini_session,
    load_session_skills,
    load_pending_inputs,
    load_goal_manager_pending_inputs,
    load_service_pending_inputs,
    list_session_agent_contacts,
    list_session_parents,
    list_session_children,
    list_sessions,
    release_session_service,
    resolve_session_agent_id,
    read_json_file,
    schedule_session_auto_resume,
    save_agent_audit_state,
    save_claude_session,
    save_codex_session,
    save_gemini_session,
    record_session_user_response_request,
    session_goal_manager_state_path,
    session_dir,
    session_skill_file_path,
    session_goal_context,
    session_timeline_path,
    sync_communication_goal_progress,
    update_goal_manager_review_cursor,
    update_session_goal_flags,
    update_session_user_response_wait,
    write_json_file,
)
from runtime.providers import run_claude, run_codex, run_gemini
from runtime.session_view import (
    is_canonical_llm_service_id,
    persisted_goal_manager_runtime_state,
    session_agent_assignment_counts,
    session_has_active_in_progress_goal,
    worker_slot_badge,
)
from runtime.service_control import (
    build_interactive_prompt,
    build_prompt,
    parse_service_response_with_fallback,
)
from runtime.status_events import publish_goal_status_changed
from wire.protocol import (
    decode_line,
    encode_line,
    make_message,
    message_meta_get,
    message_set_meta,
    utc_ts,
    write_jsonl,
)


def _ensure_dispatch_allowed_peer(
    runtime_root: Path,
    *,
    from_service_id: str,
    to_service_id: str,
) -> None:
    sender = str(from_service_id or "").strip()
    recipient = str(to_service_id or "").strip()
    if not sender or not recipient or sender == recipient:
        return
    if sender in {"user.local", "kernel.local"}:
        return
    if recipient in {"kernel.spawn", "kernel.control"}:
        return
    try:
        add_allowed_peer(runtime_root, service_id=sender, peer_service_id=recipient)
    except Exception:
        return


def _worker_session_skills_block(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
) -> str:
    skills = load_session_skills(runtime_root, username=username, session_id=session_id)
    if not skills:
        return ""
    lines = ["<aize_session_skills>"]
    for index, skill in enumerate(skills, start=1):
        skill_id = html.escape(str(skill.get("skill_id") or ""), quote=True)
        kind = html.escape(str(skill.get("kind") or "general"), quote=True)
        title = html.escape(str(skill.get("title") or skill.get("skill_id") or "Skill"))
        lines.append(f'  <skill index="{index}" id="{skill_id}" kind="{kind}">')
        lines.append(f"    <title>{title}</title>")
        description = str(skill.get("description") or "").strip()
        if description:
            lines.append(f"    <description>{html.escape(description)}</description>")
        prompt = str(skill.get("prompt") or "").strip()
        if prompt:
            lines.append(f"    <prompt>{html.escape(prompt)}</prompt>")
        when_to_use = str(skill.get("when_to_use") or "").strip()
        if when_to_use:
            lines.append(f"    <when_to_use>{html.escape(when_to_use)}</when_to_use>")
        usage = str(skill.get("usage") or "").strip()
        if usage:
            lines.append(f"    <usage>{html.escape(usage)}</usage>")
        routing_tags = [str(item).strip() for item in skill.get("routing_tags", []) if str(item).strip()]
        if routing_tags:
            lines.append(
                f"    <routing_tags>{html.escape(', '.join(routing_tags))}</routing_tags>"
            )
        files = skill.get("files") if isinstance(skill.get("files"), list) else []
        for file_entry in files:
            if not isinstance(file_entry, dict):
                continue
            relative_path = str(file_entry.get("path") or "").strip()
            if not relative_path:
                continue
            lines.append(f'    <file path="{html.escape(relative_path, quote=True)}">')
            description = str(file_entry.get("description") or "").strip()
            if description:
                lines.append(f"      <description>{html.escape(description)}</description>")
            file_path = session_skill_file_path(
                runtime_root,
                username=username,
                session_id=session_id,
                relative_path=relative_path,
            )
            try:
                content = file_path.read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                content = ""
            if content:
                lines.append("      <content>")
                lines.append(html.escape(content))
                lines.append("      </content>")
            lines.append("    </file>")
        lines.append("  </skill>")
    lines.append("</aize_session_skills>")
    return "\n".join(lines)


_USAGE_LIMIT_RETRY_RE = re.compile(r"try again at ([0-9]{1,2}:[0-9]{2}\s*[AP]M)", re.IGNORECASE)
_USER_RESPONSE_WAIT_RE = re.compile(
    r"<aize_user_response_wait>(?P<body>[\s\S]*?)</aize_user_response_wait>",
    re.IGNORECASE,
)


def _interactive_recent_context(history: list[dict[str, Any]], *, limit: int = 8) -> list[dict[str, str]]:
    context: list[dict[str, str]] = []
    for entry in history:
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        event_type = str(entry.get("event_type") or "").strip()
        direction = str(entry.get("direction") or "").strip()
        service_id = str(entry.get("service_id") or entry.get("from") or "").strip()
        lower_text = text.lower()
        if event_type in {"agent.turn_started", "item.started", "thread.started", "turn.started"}:
            continue
        if lower_text in {"response started", "item.started"} or lower_text.startswith("item.completed: command_execution"):
            continue
        role = ""
        if event_type in {
            "service.goal_manager_compact_completed",
            "service.goal_manager_compact_failed",
            "service.goal_manager_compact_failed",
            "service.goal_child_session_requests_queued",
            "service.goal_child_sessions_created",
        }:
            role = "GoalManager"
        elif event_type == "interactive.worker_completed":
            role = "WorkerAgent"
        elif direction == "in":
            role = "Agent"
        elif direction == "agent" and event_type.startswith("service.goal_"):
            role = "GoalManager"
        if not role:
            continue
        if service_id:
            role = f"{role}({service_id})"
        context.append(
            {
                "role": role,
                "ts": str(entry.get("ts") or ""),
                "text": text[:1200],
            }
        )
    return context[-max(1, limit):]


def _interactive_resume_xml(*, request_id: str, worker_text: str, source_user_text: str) -> str:
    return (
        f'<aize_resume target_role="interactive_agent" source_role="worker_agent" '
        f'reason="worker_completed" request_id="{html.escape(request_id, quote=True)}">\n'
        f"<original_user_message>{html.escape(source_user_text)}</original_user_message>\n"
        f"<worker_result>{html.escape(worker_text)}</worker_result>\n"
        "<instruction>Summarize the worker result for the user in concise Japanese. "
        "Do not mention raw XML.</instruction>\n"
        "</aize_resume>"
    )


def _interactive_worker_result_fallback_text(text: str) -> str:
    match = re.search(r"<worker_result>(?P<body>[\s\S]*?)</worker_result>", str(text or ""), re.IGNORECASE)
    if not match:
        return ""
    return html.unescape(match.group("body") or "").strip()


def _provider_event_has_user_visible_agent_text(event: dict[str, Any]) -> bool:
    event_type = str(event.get("type") or "").strip()
    if event_type == "agent_message.delta":
        return bool(str(event.get("delta") or "").strip())
    if event_type != "item.completed":
        return False
    item = event.get("item")
    if not isinstance(item, dict):
        return False
    return str(item.get("type") or "").strip() == "agent_message" and bool(str(item.get("text") or "").strip())


def _provider_from_service_id(service_id: str, *, default: str = "codex") -> str:
    normalized = str(service_id or "").strip().lower()
    for provider in ("gemini", "claude", "codex"):
        if provider in normalized:
            return provider
    return default


def _should_enqueue_post_turn_goal_manager_followup(
    *,
    provider_session_slot: str | None,
    turn_completed_input_present: bool,
    goal_input_present: bool,
    actionable_input_present: bool,
    goal_text: str,
    goal_active: bool,
    goal_completed: bool,
    goal_progress_state: str,
    goal_audit_state: str,
    user_response_wait_active: bool,
    communication_agent_enabled: bool,
    visible_text_present: bool,
    spawn_request_count: int,
) -> bool:
    if str(provider_session_slot or "").strip().lower() == "goal_manager":
        return False
    if (
        communication_agent_enabled
        and not actionable_input_present
        and int(spawn_request_count or 0) <= 0
    ):
        return False
    return bool(
        turn_completed_input_present
        and goal_text
        and goal_active
        and not goal_completed
        and goal_progress_state == "in_progress"
        and goal_audit_state in {"all_clear", "needs_compact"}
        and not user_response_wait_active
    )


def _should_advance_goal_manager_review_cursor_without_followup(
    *,
    communication_agent_enabled: bool,
    actionable_input_present: bool,
    spawn_request_count: int,
    goal_should_continue: bool,
) -> bool:
    return bool(
        communication_agent_enabled
        and not goal_should_continue
        and not actionable_input_present
        and int(spawn_request_count or 0) <= 0
    )


def _actionable_post_turn_input_present(
    incoming_text: str,
    *,
    communication_agent_enabled: bool,
) -> bool:
    if (
        batch_has_input_kind(incoming_text, "user_message")
        or batch_has_input_kind(incoming_text, "interactive_worker_result")
        or batch_has_input_kind(incoming_text, "goal_child_session_request")
    ):
        return True
    if communication_agent_enabled:
        return False
    return bool(
        batch_has_input_kind(incoming_text, "restart_resume")
        or batch_has_input_kind(incoming_text, "scheduled_resume")
    )


def _should_append_session_turn_completed_input(
    *,
    communication_agent_enabled: bool,
    actionable_input_present: bool,
    spawn_request_count: int,
) -> bool:
    if (
        communication_agent_enabled
        and not actionable_input_present
        and int(spawn_request_count or 0) <= 0
    ):
        return False
    return True


def _ensure_in_progress_goal_has_followup_owner(
    *,
    progress_state: str | None,
    audit_state: str | None,
    goal_manager_service_id: str,
    directives: list[dict[str, Any]] | None,
    child_goal_requests: list[dict[str, Any]] | None,
    user_response_requests: list[dict[str, Any]] | None,
    summary: str,
) -> list[dict[str, Any]]:
    normalized_directives = [
        dict(item)
        for item in (directives or [])
        if isinstance(item, dict) and str(item.get("service_id") or "").strip()
    ]
    if normalized_directives:
        return normalized_directives
    if str(progress_state or "").strip().lower() != "in_progress":
        return normalized_directives
    if str(audit_state or "").strip().lower() != "all_clear":
        return normalized_directives
    if any(isinstance(item, dict) for item in (child_goal_requests or [])):
        return normalized_directives
    if any(isinstance(item, dict) for item in (user_response_requests or [])):
        return normalized_directives
    owner_service_id = str(goal_manager_service_id or "").strip()
    if not owner_service_id:
        return normalized_directives
    fallback_summary = str(summary or "").strip() or (
        "Goal is still in progress. Do not leave the session without an active owner."
    )
    normalized_directives.append(
        {
            "service_id": owner_service_id,
            "audit_state": "all_clear",
            "continue_xml": default_goal_continue_xml(summary=fallback_summary),
            "request_compact": False,
            "request_compact_reason": "",
            "summary": fallback_summary,
        }
    )
    return normalized_directives


def _normalize_continuous_communication_audit_summary(
    *,
    runtime_root: Path,
    username: str,
    session_id: str,
    goal_manager_service_id: str,
    session_settings: dict[str, Any] | None,
    audit: dict[str, Any] | None,
) -> dict[str, Any] | None:
    def _session_pending_requires_summary_preservation(entries: list[dict[str, Any]]) -> bool:
        for item in entries:
            if not isinstance(item, dict):
                return True
            kind = str(item.get("kind") or "").strip().lower()
            if kind not in {"restart_resume", "scheduled_resume"}:
                return True
        return False

    if not isinstance(audit, dict):
        return audit
    if not is_continuous_communication_session(session_settings):
        return audit
    if str(audit.get("audit_state") or "").strip().lower() != "all_clear":
        return audit
    if str(audit.get("progress_state") or "").strip().lower() != "in_progress":
        return audit
    if bool((session_settings or {}).get("user_response_wait_active", False)):
        return audit
    if bool((session_settings or {}).get("waiting_on_children", False)):
        return audit
    if any(isinstance(item, dict) for item in (audit.get("child_goal_requests") or [])):
        return audit
    if any(isinstance(item, dict) for item in (audit.get("user_response_requests") or [])):
        return audit
    if _session_pending_requires_summary_preservation(
        load_pending_inputs(runtime_root, username=username, session_id=session_id)
    ):
        return audit
    if load_goal_manager_pending_inputs(runtime_root, username=username, session_id=session_id):
        return audit
    for contact in list_session_agent_contacts(runtime_root, username=username, session_id=session_id):
        if not isinstance(contact, dict):
            continue
        contact_service_id = str(contact.get("service_id") or "").strip()
        if not contact_service_id:
            continue
        contact_agent_id = str(contact.get("agent_id") or "").strip() or None
        if load_service_pending_inputs(
            runtime_root,
            service_id=contact_service_id,
            agent_id=contact_agent_id,
            username=username,
            session_id=session_id,
        ):
            return audit
    normalized = dict(audit)
    normalized["summary"] = (
        "No user work is pending to proxy, track, or report. "
        "Entrance remains active as a continuous communication session."
    )
    return normalized


def _load_or_repair_goal_manager_pending_inputs(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
) -> list[dict[str, Any]]:
    pending = load_goal_manager_pending_inputs(
        runtime_root,
        username=username,
        session_id=session_id,
    )
    if pending:
        return pending
    goal_manager_state = read_json_file(
        session_goal_manager_state_path(
            runtime_root,
            username=username,
            session_id=session_id,
        )
    ) or {}
    pending_work_items = goal_manager_state.get("pending_work_items")
    if not isinstance(pending_work_items, list):
        return []
    repaired = False
    for item in pending_work_items:
        if not isinstance(item, dict):
            continue
        append_goal_manager_pending_input(
            runtime_root,
            username=username,
            session_id=session_id,
            entry=dict(item),
        )
        repaired = True
    if not repaired:
        return []
    return load_goal_manager_pending_inputs(
        runtime_root,
        username=username,
        session_id=session_id,
    )


def _running_llm_service_pools(
    *,
    runtime_root: Path,
    manifest: dict[str, Any],
) -> dict[str, list[str]]:
    pools: dict[str, list[str]] = {"codex": [], "claude": [], "gemini": []}
    manifest_kinds = {
        str(service.get("service_id") or ""): str(service.get("kind") or "").strip().lower()
        for service in manifest.get("services", [])
        if isinstance(service, dict)
    }
    for record in list_service_records(runtime_root):
        service_id = str(record.get("service_id") or "").strip()
        kind = str(record.get("kind") or manifest_kinds.get(service_id) or "").strip().lower()
        if not service_id or kind not in pools or not is_canonical_llm_service_id(service_id):
            continue
        process_id = str(record.get("current_process_id") or "").strip()
        try:
            process = get_process_record(runtime_root, process_id) if process_id else {}
        except KeyError:
            process = {}
        if (
            str(record.get("status") or "").strip().lower() == "running"
            and isinstance(process, dict)
            and str(process.get("status") or "").strip().lower() == "running"
        ):
            pools[kind].append(service_id)
    for provider, service_ids in list(pools.items()):
        pools[provider] = sorted(set(service_ids))
    return pools


def _maybe_enqueue_in_progress_goal_lifecycle_review(
    *,
    runtime_root: Path,
    manifest: dict[str, Any],
    process_id: str,
    service_id: str,
    provider_session_slot: str,
    username: str | None,
    session_id: str | None,
    default_provider: str,
    send_tx: Callable[[dict[str, Any]], None],
    reason: str,
) -> dict[str, Any] | None:
    normalized_username = str(username or "").strip()
    normalized_session_id = str(session_id or "").strip()
    if not (normalized_username and normalized_session_id):
        return None
    if str(provider_session_slot or "").strip().lower() == "goal_manager":
        return None
    session = get_session_settings(
        runtime_root,
        username=normalized_username,
        session_id=normalized_session_id,
    ) or {}
    if not session_has_active_in_progress_goal(session):
        return None
    if bool(session.get("user_response_wait_active", False)):
        return None
    if bool(session.get("waiting_on_children", False)):
        return None

    bound_service_id = str(session.get("service_id") or "").strip()
    goal_manager_runtime = persisted_goal_manager_runtime_state(
        runtime_root,
        username=normalized_username,
        session_id=normalized_session_id,
        bound_service_id=bound_service_id,
    )
    goal_manager_service_id = str(goal_manager_runtime.get("service_id") or bound_service_id).strip()
    goal_manager_state = str(goal_manager_runtime.get("state") or "idle").strip().lower() or "idle"
    goal_manager_worker = (
        worker_slot_badge(
            goal_manager_service_id,
            codex_service_pool=[],
            claude_service_pool=[],
            gemini_service_pool=[],
        )
        if goal_manager_service_id
        else None
    )
    agent_counts = session_agent_assignment_counts(
        session,
        worker=None,
        agent_running=False,
        goal_manager_worker=goal_manager_worker,
        goal_manager_state=goal_manager_state,
    )
    if agent_counts["assigned_agents"] > 0 or agent_counts["goal_manager_reviewers"] > 0:
        return None

    return enqueue_goal_manager_lifecycle_review(
        runtime_root,
        manifest=manifest,
        from_service_id=service_id,
        process_id=process_id,
        username=normalized_username,
        session_id=normalized_session_id,
        reason=reason,
        source_service_id=service_id,
        service_pools_by_provider=_running_llm_service_pools(
            runtime_root=runtime_root,
            manifest=manifest,
        ),
        default_provider=default_provider,
        send_dispatch=lambda message: send_tx(message) or True,
    )


def _resolve_audit_state_after_goal_manager_compact(
    audit_state: str | None,
    compact_event: dict[str, Any] | None,
) -> str:
    normalized_audit_state = str(audit_state or "").strip().lower() or "all_clear"
    if normalized_audit_state not in {"needs_compact", "panic"}:
        return normalized_audit_state
    if compact_event is None:
        return "panic" if normalized_audit_state == "needs_compact" else normalized_audit_state
    if str(compact_event.get("type") or "").strip() == "service.goal_manager_compact_failed":
        return "panic"
    if str(compact_event.get("compaction") or "").strip() == "suppressed_by_session_setting":
        return "needs_compact" if normalized_audit_state == "needs_compact" else normalized_audit_state
    return "all_clear"


def _latest_goal_manager_turn_completed_ts(
    work_items: list[dict[str, Any]] | None,
) -> str:
    latest = ""
    for item in work_items or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("kind") or "").strip().lower() != "turn_completed":
            continue
        item_ts = str(item.get("ts") or "").strip()
        if item_ts > latest:
            latest = item_ts
    return latest


def _materialize_goal_child_sessions(
    *,
    runtime_root: Path,
    username: str,
    session_id: str,
    goal_id: str,
    goal_text: str,
    goal_manager_service_id: str,
    child_goal_requests: list[dict[str, Any]],
    dispatch_child_session: Callable[[str], str] | None = None,
) -> list[dict[str, str]]:
    normalized_requests = [
        item
        for item in child_goal_requests
        if isinstance(item, dict) and str(item.get("goal_text") or "").strip()
    ]
    source_session_settings = get_session_settings(
        runtime_root,
        username=username,
        session_id=session_id,
    ) or {}
    target_parent_session_id = _resolve_spawn_request_parent_session_id(
        runtime_root=runtime_root,
        username=username,
        session_id=session_id,
        session_settings=source_session_settings,
    )
    created_child_sessions: list[dict[str, str]] = []
    for child_request in normalized_requests:
        child_label = str(child_request.get("label") or "").strip() or "Subgoal"
        child_goal_text = str(child_request.get("goal_text") or "").strip()
        requested_service_id = str(child_request.get("service_id") or "").strip()
        requested_provider = str(child_request.get("provider") or "").strip().lower()
        preferred_provider = (
            requested_provider
            if requested_provider in {"codex", "claude", "gemini"}
            else _provider_from_service_id(requested_service_id)
        )
        child_session = create_child_conversation_session(
            runtime_root,
            username=username,
            parent_session_id=target_parent_session_id or session_id,
            label=child_label,
            goal_text=child_goal_text,
            created_by_username=GOAL_MANAGER_USERNAME,
            created_by_type="system",
            origin_session_id=session_id,
            requester_session_id=target_parent_session_id or session_id,
            origin_goal_id=goal_id,
            origin_goal_text=goal_text,
        )
        if not isinstance(child_session, dict):
            continue
        child_session_id = str(child_session.get("session_id") or "").strip()
        if not child_session_id:
            continue
        update_session_goal_flags(
            runtime_root,
            username=username,
            session_id=child_session_id,
            goal_active=True,
            goal_completed=False,
            goal_progress_state="in_progress",
            preferred_provider=preferred_provider,
        )
        if requested_service_id:
            join_session_agent(
                runtime_root,
                username=username,
                session_id=child_session_id,
                service_id=requested_service_id,
                provider=preferred_provider,
                role="agent",
                transport="goal_child_request",
            )
        dispatch_service_id = ""
        if dispatch_child_session is not None:
            try:
                dispatch_service_id = str(dispatch_child_session(child_session_id) or "").strip()
            except Exception:
                dispatch_service_id = ""
        created_child_sessions.append(
            {
                "session_id": child_session_id,
                "label": child_label,
                "provider": preferred_provider,
                "dispatch_service_id": dispatch_service_id,
            }
        )
    if created_child_sessions:
        append_user_history(
            runtime_root,
            username=username,
            session_id=session_id,
            limit=GOAL_AUDIT_HISTORY_LIMIT,
            entry={
                "direction": "agent",
                "ts": utc_ts(),
                "from": goal_manager_service_id,
                "session_id": session_id,
                "event_type": "service.goal_child_sessions_created",
                "text": f"GoalManager created {len(created_child_sessions)} child sessions.",
                "event": {
                    "type": "service.goal_child_sessions_created",
                    "children": created_child_sessions,
                    "goal_id": goal_id,
                },
            },
        )
    return created_child_sessions


def _service_can_spawn_children(
    *,
    self_service: dict[str, Any],
    auth_context: dict[str, Any] | None,
) -> bool:
    auth = auth_context if isinstance(auth_context, dict) else {}
    if auth_context_allows(auth, "spawn_service"):
        return True
    owner_capabilities = self_service.get("owner_capabilities")
    if isinstance(owner_capabilities, list):
        return "spawn_service" in {str(item) for item in owner_capabilities if isinstance(item, str)}
    return False


def _resolve_spawn_request_parent_session_id(
    *,
    runtime_root: Path,
    username: str,
    session_id: str,
    session_settings: dict[str, Any],
) -> str:
    route = next(
        (
            skill
            for skill in session_settings.get("session_skills", [])
            if isinstance(skill, dict)
            and str(skill.get("skill_id") or "").strip() == "canonical-development-routing"
            and str(skill.get("routing_mode") or "").strip().lower() == "create_child_session"
            and str(skill.get("canonical_session_key") or "").strip()
        ),
        None,
    )
    if not isinstance(route, dict):
        return session_id
    route_scope = str(route.get("route_parent_scope") or "").strip().lower()
    if route_scope not in {"root_session", "canonical", "global"}:
        return session_id
    target_template_id = str(
        route.get("target_unit_id") or route.get("target_template_id") or ""
    ).strip()
    if not target_template_id:
        return session_id
    canonical_key = str(route.get("canonical_session_key") or "").strip()
    target_label = str(route.get("target_label") or "").strip()
    candidates: list[dict[str, Any]] = []
    for candidate in list_sessions(runtime_root, username=username):
        if not isinstance(candidate, dict):
            continue
        candidate_id = str(candidate.get("session_id") or "").strip()
        if not candidate_id or candidate_id == session_id:
            continue
        launcher_template_id = str(
            candidate.get("launcher_template_id") or candidate.get("launcher_unit_id") or ""
        ).strip()
        if launcher_template_id != target_template_id:
            continue
        if str(candidate.get("parent_session_id") or "").strip() != "default":
            continue
        if target_label and str(candidate.get("label") or "").strip() != target_label:
            continue
        if canonical_key and not any(
            isinstance(skill, dict)
            and str(skill.get("canonical_session_key") or "").strip() == canonical_key
            for skill in candidate.get("session_skills", [])
            if isinstance(candidate.get("session_skills"), list)
        ):
            continue
        candidates.append(candidate)
    if not candidates:
        return session_id
    candidates.sort(
        key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
        reverse=True,
    )
    return str(candidates[0].get("session_id") or "").strip() or session_id


def _handoff_spawn_request_to_child_session(
    *,
    runtime_root: Path,
    username: str,
    session_id: str,
    goal_manager_service_id: str,
    control: dict[str, Any],
    dispatch_child_session: Callable[[str], str] | None = None,
) -> list[dict[str, str]]:
    service_spec = control.get("service")
    if not isinstance(service_spec, dict):
        return []
    initial_prompt = str(control.get("initial_prompt") or "").strip()
    if not initial_prompt:
        return []
    session_settings = get_session_settings(runtime_root, username=username, session_id=session_id) or {}
    parent_goal_id = str(
        session_settings.get("active_goal_id")
        or session_settings.get("goal_id")
        or f"spawn-fallback-{uuid.uuid4().hex[:8]}"
    ).strip()
    parent_goal_text = str(session_settings.get("goal_text") or "").strip() or initial_prompt
    target_parent_session_id = _canonical_spawn_handoff_parent_session_id(
        runtime_root=runtime_root,
        username=username,
        session_id=session_id,
    )
    if target_parent_session_id == session_id:
        target_parent_session_id = _resolve_spawn_request_parent_session_id(
            runtime_root=runtime_root,
            username=username,
            session_id=session_id,
            session_settings=session_settings,
        )
    child_request = {
        "label": str(service_spec.get("display_name") or "").strip()
        or str(service_spec.get("service_id") or "").strip()
        or "Subgoal",
        "goal_text": initial_prompt,
        "provider": str(service_spec.get("kind") or "").strip().lower(),
        "service_id": str(service_spec.get("service_id") or "").strip(),
    }
    return _materialize_goal_child_sessions(
        runtime_root=runtime_root,
        username=username,
        session_id=target_parent_session_id or session_id,
        goal_id=parent_goal_id,
        goal_text=parent_goal_text,
        goal_manager_service_id=goal_manager_service_id,
        child_goal_requests=[child_request],
        dispatch_child_session=dispatch_child_session,
    )


def _route_spawn_request_to_communication_child_session(
    *,
    runtime_root: Path,
    username: str,
    session_id: str,
    goal_manager_service_id: str,
    control: dict[str, Any],
    dispatch_child_session: Callable[[str], str] | None = None,
) -> list[dict[str, str]]:
    initial_prompt = str(control.get("initial_prompt") or "").strip()
    if not initial_prompt:
        return []
    current_session = get_session_settings(
        runtime_root,
        username=username,
        session_id=session_id,
    )
    if not isinstance(current_session, dict):
        return []
    if not (
        bool(current_session.get("communication_agent_enabled"))
        or str(current_session.get("session_ui_mode") or "").strip().lower() == "communication"
    ):
        return []
    spawn_handoff_session = dict(current_session)
    spawn_handoff_skills: list[dict[str, Any]] = []
    for skill in current_session.get("session_skills", []):
        if not isinstance(skill, dict):
            continue
        normalized_skill = dict(skill)
        if (
            str(normalized_skill.get("routing_mode") or "").strip().lower() == "create_child_session"
            and str(normalized_skill.get("canonical_session_key") or "").strip()
        ):
            normalized_skill["route_when_unhandled"] = True
        spawn_handoff_skills.append(normalized_skill)
    if spawn_handoff_skills:
        spawn_handoff_session["session_skills"] = spawn_handoff_skills
    from runtime.http_handler import _materialize_communication_routed_child_session

    routed_child = _materialize_communication_routed_child_session(
        runtime_root,
        username=username,
        current_session=spawn_handoff_session,
        prompt_text=initial_prompt,
        sessions=list_sessions(runtime_root, username=username),
    )
    if not isinstance(routed_child, dict):
        return []
    child_session_id = str(routed_child.get("session_id") or "").strip()
    if not child_session_id:
        return []
    dispatched_service_id = ""
    if dispatch_child_session is not None:
        dispatched_service_id = str(dispatch_child_session(child_session_id) or "").strip()
    return [
        {
            "session_id": child_session_id,
            "label": str(routed_child.get("label") or "").strip(),
            "service_id": dispatched_service_id,
        }
    ]


def _should_force_spawn_request_child_handoff(session_settings: dict[str, Any] | None) -> bool:
    if not isinstance(session_settings, dict):
        return False
    if not (
        bool(session_settings.get("communication_agent_enabled"))
        or str(session_settings.get("session_ui_mode") or "").strip().lower() == "communication"
    ):
        return False
    skills = session_settings.get("session_skills")
    if not isinstance(skills, list):
        return False
    return any(
        isinstance(skill, dict)
        and str(skill.get("routing_mode") or "").strip().lower() == "create_child_session"
        and str(skill.get("canonical_session_key") or "").strip()
        for skill in skills
    )


def _canonical_spawn_handoff_parent_session_id(
    *,
    runtime_root: Path,
    username: str,
    session_id: str,
) -> str:
    session_settings = get_session_settings(runtime_root, username=username, session_id=session_id) or {}
    if str(session_settings.get("session_ui_mode") or "").strip().lower() != "communication":
        return session_id
    route_skills = [
        skill
        for skill in session_settings.get("session_skills", [])
        if isinstance(skill, dict)
        and str(skill.get("routing_mode") or "").strip().lower() == "create_child_session"
        and str(skill.get("canonical_session_key") or "").strip()
    ]
    if not route_skills:
        return session_id
    sessions = list_sessions(runtime_root, username=username)
    for route in route_skills:
        canonical_key = str(route.get("canonical_session_key") or "").strip()
        target_label = str(route.get("target_label") or "").strip()
        candidates: list[tuple[tuple[int, str], str]] = []
        for candidate in sessions:
            candidate_id = str(candidate.get("session_id") or "").strip()
            if not candidate_id or candidate_id == session_id:
                continue
            skills = candidate.get("session_skills")
            if not isinstance(skills, list):
                continue
            if not any(
                isinstance(skill, dict)
                and str(skill.get("canonical_session_key") or "").strip() == canonical_key
                for skill in skills
            ):
                continue
            score = 0
            if str(candidate.get("session_group") or "").strip().lower() == "root":
                score += 4
            if target_label and str(candidate.get("label") or "").strip() == target_label:
                score += 2
            if str(candidate.get("goal_progress_state") or "").strip().lower() == "in_progress":
                score += 1
            candidates.append(((score, str(candidate.get("updated_at") or "")), candidate_id))
        if candidates:
            return sorted(candidates, key=lambda item: item[0], reverse=True)[0][1]
    return session_id


def _await_spawn_initial_prompt_route(
    runtime_root: Path,
    *,
    sender_service_id: str,
    child_service_id: str,
    timeout_seconds: float = 1.0,
) -> tuple[bool, str]:
    try:
        child_record = wait_for_service_record(
            runtime_root,
            child_service_id,
            timeout_seconds=timeout_seconds,
        )
    except (KeyError, FileNotFoundError, json.JSONDecodeError):
        return False, "spawn_registration_missing"
    if sender_service_id not in set(child_record.get("allowed_peers", [])):
        return False, "spawned_service_missing_reverse_route"
    return True, "ok"


def _dispatch_spawn_initial_prompt(
    *,
    runtime_root: Path,
    manifest: dict[str, Any],
    process_id: str,
    service_id: str,
    child_service_id: str,
    initial_prompt: str,
    run_id: str,
    send_tx: Callable[[dict[str, Any]], None],
    auth_context: dict[str, Any] | None,
    scope_username: str | None,
    scope_session_id: str | None,
) -> bool:
    normalized_prompt = str(initial_prompt or "").strip()
    normalized_username = str(scope_username or "").strip()
    normalized_session_id = str(scope_session_id or "").strip()
    if not normalized_prompt:
        return False
    if not (normalized_username and normalized_session_id):
        return False
    child_agent_id = resolve_session_agent_id(
        runtime_root,
        username=normalized_username,
        session_id=normalized_session_id,
        service_id=child_service_id,
    )
    append_service_pending_input(
        runtime_root,
        service_id=child_service_id,
        agent_id=child_agent_id,
        username=normalized_username,
        session_id=normalized_session_id,
        entry=make_aize_pending_input(
            kind="user_message",
            role="user",
            text=normalized_prompt,
        ),
    )
    _ensure_dispatch_allowed_peer(
        runtime_root,
        from_service_id=service_id,
        to_service_id=child_service_id,
    )
    send_tx(
        make_dispatch_pending_message(
            manifest=manifest,
            from_service_id=service_id,
            to_service_id=child_service_id,
            process_id=process_id,
            run_id=run_id,
            username=normalized_username,
            session_id=normalized_session_id,
            auth_context=auth_context,
            reason="spawn_initial_prompt",
            reply_to_service_id=service_id,
            session_agent_id=child_agent_id,
        )
    )
    return True


def _enqueue_goal_child_session_requests(
    *,
    runtime_root: Path,
    username: str,
    session_id: str,
    goal_manager_service_id: str,
    goal_audit_job_id: str,
    goal_id: str,
    goal_text: str,
    child_goal_requests: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    queued: list[dict[str, Any]] = []
    for child_request in child_goal_requests:
        if not isinstance(child_request, dict):
            continue
        child_goal_text = str(child_request.get("goal_text") or "").strip()
        if not child_goal_text:
            continue
        signal_payload = {
            "kind": "goal_child_session_request",
            "goal_audit_job_id": goal_audit_job_id,
            "goal_id": goal_id,
            "goal_text": goal_text,
            "request": {
                "label": str(child_request.get("label") or "").strip() or "Subgoal",
                "goal_text": child_goal_text,
                "provider": str(child_request.get("provider") or "").strip().lower(),
                "service_id": str(child_request.get("service_id") or "").strip(),
            },
        }
        append_service_pending_input(
            runtime_root,
            service_id=goal_manager_service_id,
            agent_id=resolve_session_agent_id(
                runtime_root,
                username=username,
                session_id=session_id,
                service_id=goal_manager_service_id,
            ),
            username=username,
            session_id=session_id,
            entry=make_aize_pending_input(
                kind="goal_child_session_request",
                role="system",
                text=json.dumps(signal_payload, ensure_ascii=False),
            ),
        )
        queued.append(signal_payload)
    return queued


def _is_usage_limit_error_text(text: str) -> bool:
    normalized = str(text or "").lower()
    return (
        "usage limit" in normalized
        or "rate limit" in normalized
        or "too many requests" in normalized
        or "at capacity" in normalized
    )


def _retry_after_seconds_from_error_text(text: str) -> int | None:
    if "at capacity" in str(text or "").lower():
        return 15 * 60
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
    parent_session_id = str(
        recovery_session_settings.get("recovery_source_session_id")
        or recovery_session_settings.get("source_session_id")
        or recovery_session_settings.get("parent_session_id")
        or ""
    ).strip()
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


def _completed_recovery_audit_if_parent_resumed(
    *,
    runtime_root: Path,
    username: str,
    session_id: str,
    recovery_session_settings: dict[str, Any],
    goal_id: str,
    goal_text: str,
) -> dict[str, Any] | None:
    if str(recovery_session_settings.get("session_group") or "").strip().lower() != "error":
        return None
    parent_resumed, reason = _parent_resume_validated_for_recovery_completion(
        runtime_root=runtime_root,
        username=username,
        recovery_session_settings=recovery_session_settings,
    )
    if not parent_resumed or reason:
        return None
    return {
        "goal_audit_session_id": session_id,
        "goal_audit_provider_session_id": session_id,
        "goal_audit_conversation_session_id": "",
        "goal_id": goal_id,
        "goal_text": goal_text,
        "progress_state": "complete",
        "audit_state": "all_clear",
        "goal_satisfied": True,
        "summary": "Recovery session auto-completed because the parent session already resumed after recovery creation.",
        "continue_xml": "",
        "request_compact": False,
        "request_compact_reason": "",
        "agent_directives": [],
        "pending_turn_completed_events": [],
        "child_goal_requests": [],
        "user_response_requests": [],
    }


def _extract_user_response_wait_control(text: str) -> tuple[str, dict[str, Any] | None]:
    raw_text = str(text or "")
    match = _USER_RESPONSE_WAIT_RE.search(raw_text)
    if not match:
        return raw_text.strip(), None
    body = match.group("body") or ""
    timeout_match = re.search(
        r"<timeout_seconds>\s*([0-9]{1,6})\s*</timeout_seconds>",
        body,
        flags=re.IGNORECASE,
    )
    timeout_seconds = 300
    if timeout_match:
        try:
            timeout_seconds = max(60, int(timeout_match.group(1)))
        except (TypeError, ValueError):
            timeout_seconds = 300
    request_id_match = re.search(
        r"<request_id>\s*([^<]{1,200})\s*</request_id>",
        body,
        flags=re.IGNORECASE,
    )
    reason_match = re.search(
        r"<reason>\s*(.*?)\s*</reason>",
        body,
        flags=re.IGNORECASE | re.DOTALL,
    )
    visible_text = (raw_text[: match.start()] + raw_text[match.end() :]).strip()
    control: dict[str, Any] = {"timeout_seconds": timeout_seconds}
    if request_id_match:
        request_id = str(request_id_match.group(1) or "").strip()
        if request_id:
            control["request_id"] = request_id
    if reason_match:
        request_reason = str(reason_match.group(1) or "").strip()
        if request_reason:
            control["request_reason"] = request_reason
    return visible_text, control


def _should_defer_dispatch_for_completed_goal(
    *,
    session_settings: dict[str, Any] | None,
    pending_inputs: list[dict[str, Any]] | None,
) -> bool:
    if not isinstance(session_settings, dict):
        return False
    goal_active = bool(session_settings.get("goal_active", False))
    goal_progress_state = str(
        session_settings.get(
            "goal_progress_state",
            "complete" if bool(session_settings.get("goal_completed", False)) else "in_progress",
        )
    ).strip().lower()
    if not (goal_active and goal_progress_state == "complete"):
        return False
    for entry in pending_inputs or []:
        if str((entry or {}).get("kind", "")).strip().lower() in {"user_message", "restart_resume", "scheduled_resume"}:
            return False
    return True


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
    if str(session_settings.get("session_group") or "").strip().lower() != "error":
        return

    parent_session_id = str(
        session_settings.get("recovery_source_session_id")
        or session_settings.get("source_session_id")
        or session_settings.get("parent_session_id")
        or ""
    ).strip()
    if not parent_session_id or parent_session_id == scope_session_id:
        return
    parent_resumed, _parent_resume_reason = _parent_resume_validated_for_recovery_completion(
        runtime_root=runtime_root,
        username=scope_username,
        recovery_session_settings=session_settings,
    )
    if parent_resumed:
        return

    parent_session_settings = get_session_settings(
        runtime_root,
        username=scope_username,
        session_id=parent_session_id,
    )
    if not isinstance(parent_session_settings, dict):
        return
    dispatch_service_id = str(parent_session_settings.get("service_id") or "").strip()
    if not dispatch_service_id:
        preferred_provider = str(
            parent_session_settings.get("preferred_provider")
            or _provider_from_service_id(str(session_settings.get("service_id") or service_id))
            or "codex"
        ).strip().lower()
        provider_pool = [
            str(candidate.get("service_id") or "").strip()
            for candidate in manifest.get("services", [])
            if isinstance(candidate, dict) and str(candidate.get("kind") or "").strip().lower() == preferred_provider
        ]
        provider_pool = [candidate for candidate in provider_pool if candidate]
        if provider_pool:
            dispatch_service_id = str(
                lease_session_service(
                    runtime_root,
                    username=scope_username,
                    session_id=parent_session_id,
                    pool_service_ids=provider_pool,
                )
                or ""
            ).strip()
    if not dispatch_service_id:
        dispatch_service_id = str(session_settings.get("service_id") or service_id).strip()
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
    _ensure_dispatch_allowed_peer(
        runtime_root,
        from_service_id=service_id,
        to_service_id=dispatch_service_id,
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


def _finalize_superseded_panic_recovery_siblings(
    *,
    runtime_root: Path,
    username: str,
    completed_recovery_session_id: str,
    completed_recovery_session_settings: dict[str, Any],
    completion_service_id: str,
) -> list[str]:
    if str(completed_recovery_session_settings.get("session_group") or "").strip().lower() != "error":
        return []
    recovery_source_session_id = str(
        completed_recovery_session_settings.get("recovery_source_session_id") or ""
    ).strip()
    recovery_panic_service_id = str(
        completed_recovery_session_settings.get("recovery_panic_service_id") or ""
    ).strip()
    if not recovery_source_session_id or not recovery_panic_service_id:
        return []
    finalized: list[str] = []
    for sibling_session_id in list_session_children(
        runtime_root,
        username=username,
        session_id=recovery_source_session_id,
    ):
        if sibling_session_id == completed_recovery_session_id:
            continue
        sibling_session = get_session_settings(
            runtime_root,
            username=username,
            session_id=sibling_session_id,
        )
        if not isinstance(sibling_session, dict):
            continue
        sibling_progress_state = str(
            sibling_session.get(
                "goal_progress_state",
                "complete" if bool(sibling_session.get("goal_completed", False)) else "in_progress",
            )
        ).strip().lower()
        if not (
            str(sibling_session.get("session_group") or "").strip().lower() == "error"
            and str(sibling_session.get("recovery_source_session_id") or "").strip() == recovery_source_session_id
            and str(sibling_session.get("recovery_panic_service_id") or "").strip() == recovery_panic_service_id
            and bool(sibling_session.get("goal_active", False))
            and sibling_progress_state == "in_progress"
        ):
            continue
        update_session_goal_flags(
            runtime_root,
            username=username,
            session_id=sibling_session_id,
            goal_active=False,
            goal_completed=True,
            goal_progress_state="complete",
        )
        append_user_history(
            runtime_root,
            username=username,
            session_id=sibling_session_id,
            entry={
                "direction": "event",
                "ts": utc_ts(),
                "service_id": completion_service_id,
                "event_type": "service.panic_recovery_superseded",
                "text": "Recovery session superseded by a completed recovery for the same source panic.",
                "event": {
                    "type": "service.panic_recovery_superseded",
                    "source_session_id": recovery_source_session_id,
                    "completed_recovery_session_id": completed_recovery_session_id,
                },
            },
            limit=GOAL_AUDIT_HISTORY_LIMIT,
        )
        finalized.append(sibling_session_id)
    return finalized


def _goal_update_xml(
    *,
    goal_id: str,
    goal_text: str,
    previous_goal_id: str = "",
    previous_goal_text: str = "",
    goal_context: list[dict[str, str]] | None = None,
) -> str:
    lines = ["<aize_goal_update>"]
    if previous_goal_id:
        lines.append(f"  <previous_goal_id>{html.escape(previous_goal_id)}</previous_goal_id>")
    if previous_goal_text:
        lines.append(f"  <previous_goal>{html.escape(previous_goal_text)}</previous_goal>")
    if goal_id:
        lines.append(f"  <goal_id>{html.escape(goal_id)}</goal_id>")
    lines.append(f"  <goal_text>{html.escape(goal_text)}</goal_text>")
    if goal_context:
        lines.append("  <goal_context>")
        for item in goal_context:
            item_goal_id = str(item.get("goal_id") or "").strip()
            item_goal_text = str(item.get("goal_text") or "").strip()
            item_goal_created_at = str(item.get("goal_created_at") or "").strip()
            if not item_goal_id or not item_goal_text:
                continue
            lines.append("    <goal>")
            lines.append(f"      <goal_id>{html.escape(item_goal_id)}</goal_id>")
            if item_goal_created_at:
                lines.append(f"      <created_at>{html.escape(item_goal_created_at)}</created_at>")
            lines.append(f"      <goal_text>{html.escape(item_goal_text)}</goal_text>")
            lines.append("    </goal>")
        lines.append("  </goal_context>")
    lines.append("  <instruction>Review the active goal and continue work toward it until GoalManager can mark it completed.</instruction>")
    lines.append("</aize_goal_update>")
    return "\n".join(lines)


def _child_session_broadcast_json(
    *,
    event_type: str,
    parent_session_id: str,
    child_session_id: str,
    parent_goal_id: str = "",
    child_goal_id: str = "",
    child_goal_text: str = "",
    child_label: str = "",
    dispatch_service_id: str = "",
    summary: str = "",
) -> str:
    # Broadcast payloads back the corresponding session_input kinds:
    # kind="child_session_created" and kind="child_session_completed".
    return json.dumps(
        {
            "type": event_type,
            "parent_session_id": parent_session_id,
            "child_session_id": child_session_id,
            "parent_goal_id": parent_goal_id,
            "child_goal_id": child_goal_id,
            "child_goal_text": child_goal_text,
            "child_label": child_label,
            "dispatch_service_id": dispatch_service_id,
            "summary": summary,
        },
        ensure_ascii=False,
    )


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
    default_provider = (
        str(config.get("default_provider") or self_service.get("kind") or "codex").strip().lower()
        or "codex"
    )
    history_limit = int(config.get("history_limit", 500))
    max_turns = int(self_service.get("max_turns", 100) or 0)
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
        preferred_provider = _provider_from_service_id(panic_service_id)
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
        existing_recovery_inputs = load_pending_inputs(
            runtime_root,
            username=username,
            session_id=recovery_session_id,
        )
        if not any(str(item.get("kind") or "") == "panic_recovery" for item in existing_recovery_inputs):
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
        recovery_provider = str(recovery_session.get("preferred_provider") or "").strip().lower()
        dispatch_service_id = ""
        if recovery_provider:
            recovery_pool = _pool_for_kind_from_manifest(recovery_provider)
            dispatch_service_id = str(
                lease_session_service(
                    runtime_root,
                    username=username,
                    session_id=recovery_session_id,
                    pool_service_ids=recovery_pool,
                )
                or ""
            ).strip()
            if dispatch_service_id:
                join_session_agent(
                    runtime_root,
                    username=username,
                    session_id=recovery_session_id,
                    service_id=dispatch_service_id,
                    agent_id=resolve_session_agent_id(
                        runtime_root,
                        username=username,
                        session_id=recovery_session_id,
                        service_id=dispatch_service_id,
                    ),
                    provider=recovery_provider,
                    role="agent",
                    transport="panic_recovery",
                )
        if not dispatch_service_id:
            dispatch_service_id = panic_service_id or service_id
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
            to_service_id=dispatch_service_id,
            process_id=process_id,
            run_id=f"panic-recovery-{uuid.uuid4().hex[:8]}",
            username=username,
            session_id=recovery_session_id,
            auth_context=None,
            reason="panic_recovery",
            session_agent_id=resolve_session_agent_id(
                runtime_root,
                username=username,
                session_id=recovery_session_id,
                service_id=dispatch_service_id,
            ),
            dispatch_priority=dispatch_priority("panic_recovery"),
        )
        _ensure_dispatch_allowed_peer(
            runtime_root,
            from_service_id=service_id,
            to_service_id=dispatch_service_id,
        )
        send_tx(dispatch_message)
        queue_parent_child_state_change(
            username=username,
            child_session_id=session_id,
            event_kind="child_session_panic",
            summary=(
                f"Child session '{session_label}' entered panic recovery. "
                f"Recovery session created: {recovery_session_id}"
            ),
            source_service_id=service_id,
            source_event={
                "type": "child_session_panic",
                "source_session_id": session_id,
                "recovery_session_id": recovery_session_id,
                "panic_service_id": panic_service_id,
                "panic_event": dict(panic_event or {}),
            },
        )
        return recovery_session

    def _pool_for_kind_from_manifest(kind: str) -> list[str]:
        """Derive service pool for a provider kind from live registry, falling back to the manifest."""
        registry_pool = [
            str(service.get("service_id"))
            for service in list_service_records(runtime_root)
            if isinstance(service, dict)
            and isinstance(service.get("service_id"), str)
            and str(service.get("kind") or "") == kind
            and str(service.get("status") or "").strip().lower() == "running"
        ]
        if registry_pool:
            return sorted(registry_pool)
        return [
            s["service_id"]
            for s in manifest.get("services", [])
            if isinstance(s.get("service_id"), str) and s.get("kind") == kind
        ]

    def _available_dispatch_kinds(*, include_external: bool) -> set[str]:
        native_kinds = {"codex", "claude", "gemini"}
        kinds: set[str] = set(native_kinds)
        if not include_external:
            return kinds
        for service in list_service_records(runtime_root):
            if not isinstance(service, dict):
                continue
            kind = str(service.get("kind") or "").strip().lower()
            service_id_value = str(service.get("service_id") or "").strip()
            status = str(service.get("status") or "").strip().lower()
            if kind and service_id_value and status == "running":
                kinds.add(kind)
        for service in manifest.get("services", []):
            if not isinstance(service, dict):
                continue
            kind = str(service.get("kind") or "").strip().lower()
            if kind and isinstance(service.get("service_id"), str):
                kinds.add(kind)
        return kinds

    def _priority_allows_external(value: Any) -> bool:
        if not isinstance(value, list):
            return False
        for raw_item in value:
            item = str(raw_item or "").strip().lower()
            if item == "border":
                return False
            if item and item not in {"codex", "claude", "gemini"}:
                return True
        return False

    def resolve_session_dispatch_service(
        *,
        username: str,
        session_id: str,
        default_service_id: str | None = None,
    ) -> str | None:
        session_settings = get_session_settings(runtime_root, username=username, session_id=session_id) or {}
        agent_priority = active_agent_priority(
            session_settings.get("agent_priority"),
            available_kinds=_available_dispatch_kinds(include_external=True),
        )
        if not agent_priority:
            preferred_provider = (
                str(session_settings.get("preferred_provider") or self_service.get("kind") or "").strip().lower()
                or str(self_service.get("kind") or "codex")
            )
            agent_priority = [preferred_provider]
        current_service_id = str(session_settings.get("service_id") or "").strip()
        for provider in agent_priority:
            pool = _pool_for_kind_from_manifest(provider)
            if current_service_id and current_service_id in pool:
                return current_service_id
            leased_service_id = lease_session_service(
                runtime_root,
                username=username,
                session_id=session_id,
                pool_service_ids=pool,
            )
            if leased_service_id:
                join_session_agent(
                    runtime_root,
                    username=username,
                    session_id=session_id,
                    service_id=leased_service_id,
                    agent_id=resolve_session_agent_id(
                        runtime_root,
                        username=username,
                        session_id=session_id,
                        service_id=leased_service_id,
                    ),
                    provider=provider,
                    role="agent",
                    transport="local_dispatch",
                )
                return leased_service_id
        if isinstance(default_service_id, str) and default_service_id.strip():
            return default_service_id.strip()
        return None

    def resolve_goal_manager_dispatch_service(
        *,
        username: str,
        session_id: str,
    ) -> str | None:
        session_settings = get_session_settings(runtime_root, username=username, session_id=session_id) or {}
        goal_manager_priority = active_goal_manager_priority(
            session_settings.get("goal_manager_priority"),
            available_kinds=_available_dispatch_kinds(
                include_external=_priority_allows_external(session_settings.get("goal_manager_priority"))
            ),
        )
        if not goal_manager_priority:
            preferred_provider = (
                str(session_settings.get("preferred_provider") or self_service.get("kind") or "").strip().lower()
                or str(self_service.get("kind") or "codex")
            )
            goal_manager_priority = [preferred_provider]
        current_service_id = str(session_settings.get("service_id") or "").strip()
        for provider in goal_manager_priority:
            pool = _pool_for_kind_from_manifest(provider)
            if current_service_id and current_service_id in pool:
                return current_service_id
            leased_service_id = lease_session_service(
                runtime_root,
                username=username,
                session_id=session_id,
                pool_service_ids=pool,
            )
            if leased_service_id:
                join_session_agent(
                    runtime_root,
                    username=username,
                    session_id=session_id,
                    service_id=leased_service_id,
                    agent_id=resolve_session_agent_id(
                        runtime_root,
                        username=username,
                        session_id=session_id,
                        service_id=leased_service_id,
                    ),
                    provider=provider,
                    role="goal_manager",
                    transport="local_dispatch",
                )
                return leased_service_id
        return None

    def queue_parent_child_state_change(
        *,
        username: str,
        child_session_id: str,
        event_kind: str,
        summary: str,
        source_service_id: str,
        source_event: dict[str, Any] | None = None,
    ) -> list[str]:
        queued_parent_ids: list[str] = []
        child_session_settings = get_session_settings(
            runtime_root,
            username=username,
            session_id=child_session_id,
        ) or {}
        child_label = str(child_session_settings.get("label") or child_session_id).strip() or child_session_id
        child_goal_text = str(child_session_settings.get("goal_text") or "").strip()
        for parent_session_id in list_session_parents(
            runtime_root,
            username=username,
            session_id=child_session_id,
        ):
            parent_session_settings = get_session_settings(
                runtime_root,
                username=username,
                session_id=parent_session_id,
            ) or {}
            parent_progress_state = str(
                parent_session_settings.get(
                    "goal_progress_state",
                    "complete" if bool(parent_session_settings.get("goal_completed", False)) else "in_progress",
                )
            ).strip().lower()
            if (
                not bool(parent_session_settings.get("goal_active", False))
                or bool(parent_session_settings.get("goal_completed", False))
                or parent_progress_state != "in_progress"
            ):
                continue
            payload = {
                "event_type": event_kind,
                "parent_session_id": parent_session_id,
                "child_session_id": child_session_id,
                "child_label": child_label,
                "child_goal_text": child_goal_text,
                "summary": summary,
            }
            if isinstance(source_event, dict):
                payload["event"] = dict(source_event)
            append_pending_input(
                runtime_root,
                username=username,
                session_id=parent_session_id,
                entry=make_aize_pending_input(
                    kind=event_kind,
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
                    "kind": event_kind,
                    "ts": utc_ts(),
                    "service_id": source_service_id,
                    "text": summary,
                },
                limit=GOAL_AUDIT_HISTORY_LIMIT,
            )
            dispatch_targets: list[str] = []
            for contact in list_session_agent_contacts(runtime_root, username=username, session_id=parent_session_id):
                contact_service_id = str(contact.get("service_id") or "").strip()
                if contact_service_id and contact_service_id not in dispatch_targets:
                    dispatch_targets.append(contact_service_id)
            dispatch_service_id = resolve_session_dispatch_service(
                username=username,
                session_id=parent_session_id,
                default_service_id=str(parent_session_settings.get("service_id") or "").strip() or None,
            )
            if dispatch_service_id and dispatch_service_id not in dispatch_targets:
                dispatch_targets.append(dispatch_service_id)
            for target_service_id in dispatch_targets:
                _ensure_dispatch_allowed_peer(
                    runtime_root,
                    from_service_id=source_service_id,
                    to_service_id=target_service_id,
                )
                send_tx(
                    make_dispatch_pending_message(
                        manifest=manifest,
                        from_service_id=source_service_id,
                        to_service_id=target_service_id,
                        process_id=process_id,
                        run_id=f"{event_kind}-{uuid.uuid4().hex[:8]}",
                        username=username,
                        session_id=parent_session_id,
                        auth_context=None,
                        reason=event_kind,
                        session_agent_id=resolve_session_agent_id(
                            runtime_root,
                            username=username,
                            session_id=parent_session_id,
                            service_id=target_service_id,
                        ),
                        dispatch_priority=dispatch_priority(event_kind),
                    )
                )
            queued_parent_ids.append(parent_session_id)
        return queued_parent_ids

    def kickoff_goal_child_session_for_dispatch(
        *,
        username: str,
        parent_session_id: str,
        child_session_id: str,
        goal_manager_service_id: str,
    ) -> str:
        child_settings = get_session_settings(
            runtime_root,
            username=username,
            session_id=child_session_id,
        ) or {}
        child_goal_id = str(
            child_settings.get("active_goal_id")
            or child_settings.get("goal_id")
            or ""
        ).strip()
        child_goal_text = str(child_settings.get("goal_text") or "").strip()
        if not child_goal_text:
            return ""
        dispatch_service_id = resolve_goal_manager_dispatch_service(
            username=username,
            session_id=child_session_id,
        )
        if not dispatch_service_id:
            return ""
        append_pending_input(
            runtime_root,
            username=username,
            session_id=child_session_id,
            entry=make_aize_pending_input(
                kind="goal_update",
                role="system",
                text=_goal_update_xml(
                    goal_id=child_goal_id,
                    goal_text=child_goal_text,
                    goal_context=session_goal_context(
                        runtime_root,
                        username=username,
                        session_id=child_session_id,
                    ),
                ),
            ),
        )
        _ensure_dispatch_allowed_peer(
            runtime_root,
            from_service_id=goal_manager_service_id,
            to_service_id=dispatch_service_id,
        )
        send_tx(
            make_dispatch_pending_message(
                manifest=manifest,
                from_service_id=goal_manager_service_id,
                to_service_id=dispatch_service_id,
                process_id=process_id,
                run_id=f"goal-child-{uuid.uuid4().hex[:8]}",
                username=username,
                session_id=child_session_id,
                auth_context=None,
                reason="goal_child_session_created",
                session_agent_id=resolve_session_agent_id(
                    runtime_root,
                    username=username,
                    session_id=child_session_id,
                    service_id=dispatch_service_id,
                ),
            )
        )
        append_user_history(
            runtime_root,
            username=username,
            session_id=parent_session_id,
            limit=GOAL_AUDIT_HISTORY_LIMIT,
            entry={
                "direction": "agent",
                "ts": utc_ts(),
                "from": goal_manager_service_id,
                "session_id": parent_session_id,
                "event_type": "service.goal_child_session_dispatched",
                "text": f"GoalManager dispatched child session {child_session_id} to {dispatch_service_id}.",
                "event": {
                    "type": "service.goal_child_session_dispatched",
                    "child_session_id": child_session_id,
                    "dispatch_service_id": dispatch_service_id,
                },
            },
        )
        return dispatch_service_id

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
        agent_priority = active_agent_priority(
            session_settings.get("agent_priority"),
            available_kinds=_available_dispatch_kinds(include_external=True),
        )

        # Determine which kind the failed service is
        failed_kind = str(
            next(
                (s.get("kind") for s in manifest.get("services", []) if s.get("service_id") == failure_service_id),
                _provider_from_service_id(failure_service_id, default="codex"),
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
                            text=_goal_update_xml(
                                goal_id=active_goal_id,
                                goal_text=goal_text,
                                goal_context=session_goal_context(
                                    runtime_root,
                                    username=username,
                                    session_id=session_id,
                                ),
                            ),
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
                _ensure_dispatch_allowed_peer(
                    runtime_root,
                    from_service_id=service_id,
                    to_service_id=next_svc,
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

    def maybe_resume_parent_after_child_completion(
        *,
        username: str,
        child_session_id: str,
        child_session_settings: dict[str, Any],
        completion_service_id: str,
    ) -> dict[str, Any] | None:
        parent_session_id = str(child_session_settings.get("parent_session_id") or "").strip()
        if not parent_session_id:
            return None
        if str(child_session_settings.get("child_completion_reported_at") or "").strip():
            return None
        _finalize_superseded_panic_recovery_siblings(
            runtime_root=runtime_root,
            username=username,
            completed_recovery_session_id=child_session_id,
            completed_recovery_session_settings=child_session_settings,
            completion_service_id=completion_service_id,
        )
        progress = complete_session_child(
            runtime_root,
            username=username,
            parent_session_id=parent_session_id,
            child_session_id=child_session_id,
        )
        if not isinstance(progress, dict):
            return None
        parent_session_settings = get_session_settings(
            runtime_root,
            username=username,
            session_id=parent_session_id,
        ) or {}
        child_goal_id = str(
            child_session_settings.get("active_goal_id")
            or child_session_settings.get("goal_id")
            or ""
        ).strip()
        child_goal_text = str(child_session_settings.get("goal_text") or "").strip()
        child_label = str(child_session_settings.get("label") or child_session_id).strip() or child_session_id
        origin_session_id = str(child_session_settings.get("origin_session_id") or "").strip()
        summary = (
            f"SubGoal '{child_label}' completed. "
            f"{'Other child sessions are still running.' if progress.get('waiting_on_children') else 'No child sessions remain in progress.'}"
        )
        report_session_ids: list[str] = [parent_session_id]
        if origin_session_id and origin_session_id not in report_session_ids:
            report_session_ids.append(origin_session_id)
        for report_session_id in report_session_ids:
            report_session_settings = (
                parent_session_settings
                if report_session_id == parent_session_id
                else get_session_settings(
                    runtime_root,
                    username=username,
                    session_id=report_session_id,
                )
                or {}
            )
            payload = _child_session_broadcast_json(
                event_type="child_session_completed",
                parent_session_id=parent_session_id,
                child_session_id=child_session_id,
                parent_goal_id=str(
                    report_session_settings.get("active_goal_id")
                    or report_session_settings.get("goal_id")
                    or ""
                ).strip(),
                child_goal_id=child_goal_id,
                child_goal_text=child_goal_text,
                child_label=child_label,
                dispatch_service_id=completion_service_id,
                summary=summary,
            )
            append_pending_input(
                runtime_root,
                username=username,
                session_id=report_session_id,
                entry=make_aize_pending_input(
                    kind="child_session_completed",
                    role="system",
                    text=payload,
                ),
            )
            append_user_history(
                runtime_root,
                username=username,
                session_id=report_session_id,
                entry={
                    "direction": "session_input",
                    "kind": "child_session_completed",
                    "ts": utc_ts(),
                    "service_id": completion_service_id,
                    "text": summary,
                },
                limit=GOAL_AUDIT_HISTORY_LIMIT,
            )
            dispatch_targets: list[str] = []
            for contact in list_session_agent_contacts(runtime_root, username=username, session_id=report_session_id):
                contact_service_id = str(contact.get("service_id") or "").strip()
                if contact_service_id and contact_service_id not in dispatch_targets:
                    dispatch_targets.append(contact_service_id)
            dispatch_service_id = resolve_session_dispatch_service(
                username=username,
                session_id=report_session_id,
                default_service_id=str(report_session_settings.get("service_id") or "").strip() or None,
            )
            if dispatch_service_id and dispatch_service_id not in dispatch_targets:
                dispatch_targets.append(dispatch_service_id)
            for target_service_id in dispatch_targets:
                _ensure_dispatch_allowed_peer(
                    runtime_root,
                    from_service_id=completion_service_id,
                    to_service_id=target_service_id,
                )
                send_tx(
                    make_dispatch_pending_message(
                        manifest=manifest,
                        from_service_id=completion_service_id,
                        to_service_id=target_service_id,
                        process_id=process_id,
                        run_id=f"child-session-complete-{uuid.uuid4().hex[:8]}",
                        username=username,
                        session_id=report_session_id,
                        auth_context=None,
                        reason="child_session_completed",
                        session_agent_id=resolve_session_agent_id(
                            runtime_root,
                            username=username,
                            session_id=report_session_id,
                            service_id=target_service_id,
                        ),
                        dispatch_priority=dispatch_priority("child_session_completed"),
                    )
                )
        return progress

    def decode_goal_manager_review_inputs(
        pending_inputs: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        decoded: list[dict[str, Any]] = []
        for item in pending_inputs or []:
            if str((item or {}).get("kind") or "").strip().lower() != "goal_manager_review":
                continue
            raw_text = str((item or {}).get("text") or "").strip()
            if not raw_text:
                continue
            try:
                parsed = json.loads(raw_text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                decoded.append(parsed)
        return decoded

    def decode_goal_child_session_request_inputs(
        pending_inputs: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        decoded: list[dict[str, Any]] = []
        for item in pending_inputs or []:
            if str((item or {}).get("kind") or "").strip().lower() != "goal_child_session_request":
                continue
            raw_text = str((item or {}).get("text") or "").strip()
            if not raw_text:
                continue
            try:
                parsed = json.loads(raw_text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                decoded.append(parsed)
        return decoded

    def run_goal_manager_review(
        *,
        username: str,
        session_id: str,
        session_settings: dict[str, Any],
        goal_text: str,
        reply_index: int,
        goal_manager_service_id: str,
        gm_work_items: list[dict[str, Any]],
        append_scoped_history: Callable[[dict[str, Any], int], None],
    ) -> None:
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

        def emit_goal_status(updated_session: dict[str, Any] | None, previous_session: dict[str, Any] | None) -> None:
            if not updated_session:
                return
            publish_goal_status_changed(
                lambda history_username, history_session_id, entry: append_scoped_history(
                    entry,
                    limit=GOAL_AUDIT_HISTORY_LIMIT,
                ),
                service_id=goal_manager_service_id,
                username=username,
                session_id=session_id,
                session=updated_session,
                previous_session=previous_session,
            )

        def goal_provider_event_sink(event: dict[str, Any]) -> None:
            provider_event_type = str(event.get("type") or "event").strip() or "event"
            history_entry = make_history_event_entry(event, service_id=goal_manager_service_id)
            history_entry["direction"] = "agent"
            history_entry["from"] = goal_manager_service_id
            history_entry["session_id"] = session_id
            history_entry["event_type"] = f"service.goal_manager_compact_provider_event.{provider_event_type}"
            history_entry["event"] = {
                "type": "service.goal_manager_compact_provider_event",
                "goal_audit_job_id": goal_audit_job_id,
                "provider_event": event,
            }
            write_jsonl(
                log_path,
                {
                    "type": "service.goal_manager_compact_provider_event",
                    "ts": utc_ts(),
                    "service_id": goal_manager_service_id,
                    "process_id": process_id,
                    "goal_audit_job_id": goal_audit_job_id,
                    "scope": {"username": username, "session_id": session_id},
                    "provider_event": event,
                },
            )
            if not _provider_event_has_user_visible_agent_text(event):
                goal_history_sink(history_entry)

        started_event = {
            "type": "service.goal_manager_compact_started",
            "ts": utc_ts(),
            "service_id": goal_manager_service_id,
            "process_id": process_id,
            "goal_audit_job_id": goal_audit_job_id,
            "scope": {"username": username, "session_id": session_id},
            "goal_id": goal_id,
            "goal_text": goal_text,
            "goal_manager_work_items": gm_work_items,
        }
        turn_started_event = {
            "type": "agent.turn_started",
            "ts": utc_ts(),
            "service_id": goal_manager_service_id,
            "process_id": process_id,
            "run_id": goal_audit_job_id,
            "reply_index": reply_index,
            "scope": {"username": username, "session_id": session_id},
            "goal_manager": True,
        }
        goal_manager_state_path = session_goal_manager_state_path(
            runtime_root,
            username=username,
            session_id=session_id,
        )
        goal_manager_state = read_json_file(goal_manager_state_path) or {}
        goal_manager_state.update(
            {
                "state": "running",
                "service_id": goal_manager_service_id,
                "goal_audit_job_id": goal_audit_job_id,
                "goal_id": goal_id,
                "goal_text": goal_text,
                "pending_work_items": gm_work_items,
                "last_queued_turn_completed_at": _latest_goal_manager_turn_completed_ts(gm_work_items),
                "updated_at": utc_ts(),
            }
        )
        write_jsonl(log_path, turn_started_event)
        write_json_file(goal_manager_state_path, goal_manager_state)
        write_jsonl(log_path, started_event)
        join_session_agent(
            runtime_root,
            username=username,
            session_id=session_id,
            service_id=goal_manager_service_id,
            agent_id=resolve_session_agent_id(
                runtime_root,
                username=username,
                session_id=session_id,
                service_id=goal_manager_service_id,
            ),
            provider=str(self_service.get("kind", "")),
            role="goal_manager",
            transport="goal_manager_review",
        )
        goal_history_sink(
            {
                "direction": "event",
                "ts": utc_ts(),
                "service_id": goal_manager_service_id,
                "event_type": "agent.turn_started",
                "text": f"GoalManager {goal_manager_service_id} started reviewing",
                "event": turn_started_event,
            }
        )
        goal_history_sink(
            {
                "direction": "agent",
                "ts": utc_ts(),
                "from": goal_manager_service_id,
                "session_id": session_id,
                "event_type": "service.goal_manager_compact_started",
                "text": "GoalManager is reviewing this session.",
                "event": started_event,
            }
        )
        try:
            resolved_audit_state = load_agent_audit_state(
                runtime_root,
                service_id=goal_manager_service_id,
                username=username,
                session_id=session_id,
            )
            audit: dict[str, Any] | None = None
            compact_event: dict[str, Any] | None = None
            completed_recovery_audit = _completed_recovery_audit_if_parent_resumed(
                runtime_root=runtime_root,
                username=username,
                session_id=session_id,
                recovery_session_settings=session_settings,
                goal_id=goal_id,
                goal_text=goal_text,
            )
            if completed_recovery_audit is not None:
                audit = completed_recovery_audit
                resolved_audit_state = "all_clear"
                persist_goal_audit_completion(
                    runtime_root=runtime_root,
                    log_path=log_path,
                    service_id=goal_manager_service_id,
                    process_id=process_id,
                    goal_audit_job_id=goal_audit_job_id,
                    username=username,
                    session_id=session_id,
                    audit=audit,
                    history_sink=goal_history_sink,
                )
            elif resolved_audit_state == "all_clear":
                snapshot = get_user_history(
                    runtime_root,
                    username=username,
                    session_id=session_id,
                )
                audit = run_goal_audit(
                    runtime_root=runtime_root,
                    username=username,
                    session_id=session_id,
                    goal_text=goal_text,
                    history_entries=snapshot,
                    provider_kind=str(self_service.get("kind", "")),
                    on_event=goal_provider_event_sink,
                )
                audit = _normalize_continuous_communication_audit_summary(
                    runtime_root=runtime_root,
                    username=username,
                    session_id=session_id,
                    goal_manager_service_id=goal_manager_service_id,
                    session_settings=get_session_settings(
                        runtime_root,
                        username=username,
                        session_id=session_id,
                    )
                    or {},
                    audit=audit,
                )
                persist_goal_audit_completion(
                    runtime_root=runtime_root,
                    log_path=log_path,
                    service_id=goal_manager_service_id,
                    process_id=process_id,
                    goal_audit_job_id=goal_audit_job_id,
                    username=username,
                    session_id=session_id,
                    audit={**audit, "goal_id": goal_id, "goal_text": goal_text},
                    history_sink=goal_history_sink,
                )
                compact_event = handle_goal_manager_compact_request(
                    runtime_root=runtime_root,
                    repo_root=Path(__file__).resolve().parents[2],
                    log_path=log_path,
                    service_id=goal_manager_service_id,
                    process_id=process_id,
                    goal_audit_job_id=goal_audit_job_id,
                    username=username,
                    session_id=session_id,
                    audit=audit,
                    history_sink=goal_history_sink,
                )
                resolved_audit_state = str(audit["audit_state"]).strip().lower()
            elif resolved_audit_state == "needs_compact":
                compact_event = handle_goal_manager_compact_request(
                    runtime_root=runtime_root,
                    repo_root=Path(__file__).resolve().parents[2],
                    log_path=log_path,
                    service_id=goal_manager_service_id,
                    process_id=process_id,
                    goal_audit_job_id=goal_audit_job_id,
                    username=username,
                    session_id=session_id,
                    audit={
                        "request_compact": True,
                        "request_compact_reason": "TurnCompleted auto-compact threshold exceeded.",
                    },
                    history_sink=goal_history_sink,
                )
            resolved_audit_state = _resolve_audit_state_after_goal_manager_compact(
                resolved_audit_state,
                compact_event,
            )
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
                            username=username,
                            session_id=session_id,
                            last_turn_completed_at=max(reviewed_ts),
                        )
            if audit is not None and isinstance(audit.get("child_goal_requests"), list):
                queued_child_signals = _enqueue_goal_child_session_requests(
                    runtime_root=runtime_root,
                    username=username,
                    session_id=session_id,
                    goal_manager_service_id=goal_manager_service_id,
                    goal_audit_job_id=goal_audit_job_id,
                    goal_id=goal_id,
                    goal_text=goal_text,
                    child_goal_requests=list(audit.get("child_goal_requests", [])),
                )
                if queued_child_signals:
                    goal_history_sink(
                        {
                            "direction": "agent",
                            "ts": utc_ts(),
                            "from": goal_manager_service_id,
                            "session_id": session_id,
                            "event_type": "service.goal_child_session_requests_queued",
                            "text": f"GoalManager queued {len(queued_child_signals)} child-session signal(s).",
                            "event": {
                                "type": "service.goal_child_session_requests_queued",
                                "goal_audit_job_id": goal_audit_job_id,
                                "requests": queued_child_signals,
                            },
                        }
                    )
                    send_tx(
                        make_dispatch_pending_message(
                            manifest=manifest,
                            from_service_id=goal_manager_service_id,
                            to_service_id=goal_manager_service_id,
                            process_id=process_id,
                            run_id=f"goal-child-signal-{uuid.uuid4().hex[:8]}",
                            username=username,
                            session_id=session_id,
                            auth_context=None,
                            reason="goal_child_session_request",
                            session_agent_id=resolve_session_agent_id(
                                runtime_root,
                                username=username,
                                session_id=session_id,
                                service_id=goal_manager_service_id,
                            ),
                        )
                    )
            user_response_requests = (
                list(audit.get("user_response_requests", []))
                if audit is not None and isinstance(audit.get("user_response_requests"), list)
                else []
            )
            if user_response_requests:
                request = next((item for item in user_response_requests if isinstance(item, dict)), None)
                if request is not None:
                    wait_record = update_session_user_response_wait(
                        runtime_root,
                        username=username,
                        session_id=session_id,
                        active=True,
                        timeout_seconds=request.get("timeout_seconds"),
                        prompt_text=request.get("question"),
                        request_reason=request.get("reason"),
                        source_service_id=goal_manager_service_id,
                        requested_by_role="goal_manager",
                    )
                    request_id = str((wait_record or {}).get("user_response_wait_request_id") or "").strip()
                    generated_at = str((wait_record or {}).get("user_response_wait_generated_at") or "")
                    goal_history_sink(
                        {
                            "direction": "agent",
                            "ts": utc_ts(),
                            "from": goal_manager_service_id,
                            "session_id": session_id,
                            "event_type": "service.user_response_wait_started",
                            "text": str(request.get("question") or "").strip(),
                            "event": {
                                "type": "service.user_response_wait_started",
                                "request_id": request_id,
                                "generated_at": generated_at,
                                "timeout_seconds": int(request.get("timeout_seconds", 300) or 300),
                                "effective_timeout_seconds": int((wait_record or {}).get("user_response_wait_effective_timeout_seconds", 300) or 300),
                                "until_at": str((wait_record or {}).get("user_response_wait_until_at") or ""),
                                "prompt_text": str(request.get("question") or "").strip(),
                                "reason": str(request.get("reason") or "").strip(),
                                "source_service_id": goal_manager_service_id,
                                "requested_by_role": "goal_manager",
                            },
                        }
                    )
                    previous_session = get_session_settings(
                        runtime_root,
                        username=username,
                        session_id=session_id,
                    )
                    updated_session = update_session_goal_flags(
                        runtime_root,
                        username=username,
                        session_id=session_id,
                        goal_completed=False,
                        goal_progress_state="in_progress",
                    )
                    emit_goal_status(updated_session, previous_session)
                    return
            session_settings = get_session_settings(
                runtime_root,
                username=username,
                session_id=session_id,
            ) or {}
            if audit_progress_state == "complete":
                updated_session = update_session_goal_flags(
                    runtime_root,
                    username=username,
                    session_id=session_id,
                    goal_completed=True,
                    goal_progress_state="complete",
                )
                emit_goal_status(updated_session, session_settings)
            elif resolved_audit_state == "panic":
                save_agent_audit_state(
                    runtime_root,
                    service_id=goal_manager_service_id,
                    username=username,
                    session_id=session_id,
                    audit_state="panic",
                )
                updated_session = update_session_goal_flags(
                    runtime_root,
                    username=username,
                    session_id=session_id,
                    goal_completed=False,
                    goal_progress_state="in_progress",
                )
                emit_goal_status(updated_session, session_settings)
            else:
                save_agent_audit_state(
                    runtime_root,
                    service_id=goal_manager_service_id,
                    username=username,
                    session_id=session_id,
                    audit_state=resolved_audit_state,
                )
                if not _should_preserve_prompt_cycle_progress_during_goal_review(
                    session_settings,
                    audit_progress_state=audit_progress_state,
                    resolved_audit_state=resolved_audit_state,
                ):
                    updated_session = update_session_goal_flags(
                        runtime_root,
                        username=username,
                        session_id=session_id,
                        goal_completed=False,
                        goal_progress_state="in_progress",
                    )
                    emit_goal_status(updated_session, session_settings)
            latest_session_settings = get_session_settings(
                runtime_root,
                username=username,
                session_id=session_id,
            ) or {}
            raw_agent_directives = (
                list(audit.get("agent_directives", []))
                if audit is not None and isinstance(audit.get("agent_directives"), list)
                else []
            )
            normalized_directives: list[dict[str, Any]] = []
            for directive in raw_agent_directives:
                if not isinstance(directive, dict):
                    continue
                directive_service_id = str(directive.get("service_id") or "").strip()
                if not directive_service_id:
                    continue
                normalized_directives.append(
                    {
                        "service_id": directive_service_id,
                        "audit_state": str(directive.get("audit_state") or "all_clear").strip().lower(),
                        "continue_xml": str(directive.get("continue_xml") or "").strip(),
                        "request_compact": bool(directive.get("request_compact", False)),
                        "request_compact_reason": str(directive.get("request_compact_reason") or "").strip(),
                        "summary": str(directive.get("summary") or "").strip(),
                    }
                )
            normalized_directives = _ensure_in_progress_goal_has_followup_owner(
                progress_state=audit_progress_state,
                audit_state=resolved_audit_state,
                goal_manager_service_id=goal_manager_service_id,
                directives=normalized_directives,
                child_goal_requests=(
                    list(audit.get("child_goal_requests", []))
                    if audit is not None and isinstance(audit.get("child_goal_requests"), list)
                    else []
                ),
                user_response_requests=user_response_requests,
                summary=str(audit.get("summary") or "").strip() if audit is not None else "",
            )
            if resolved_audit_state == "panic" and compact_event is not None:
                spawn_panic_recovery(
                    username=username,
                    session_id=session_id,
                    panic_event=compact_event,
                    panic_service_id=goal_manager_service_id,
                )
            if not goal_audit_should_enqueue_agent_followup(
                progress_state=audit_progress_state,
                audit_state=resolved_audit_state,
                agent_directives=normalized_directives,
            ):
                return
            agent_welcome_enabled = bool(latest_session_settings.get("agent_welcome_enabled", False))
            if not agent_welcome_enabled:
                normalized_directives = [
                    directive
                    for directive in normalized_directives
                    if str(directive.get("service_id") or "").strip() == goal_manager_service_id
                ]
            for directive in normalized_directives:
                directive_state = str(directive.get("audit_state") or "all_clear").strip().lower()
                if directive_state not in {"all_clear", "needs_compact", "panic"}:
                    directive_state = "all_clear"
                if (
                    directive_state == "needs_compact"
                    and resolved_audit_state == "all_clear"
                    and bool(directive.get("request_compact", False))
                ):
                    directive_state = "all_clear"
                save_agent_audit_state(
                    runtime_root,
                    service_id=str(directive["service_id"]),
                    username=username,
                    session_id=session_id,
                    audit_state=directive_state,
                )
            contacted_agents = list_session_agent_contacts(
                runtime_root,
                username=username,
                session_id=session_id,
            )
            dispatch_targets = goal_followup_dispatch_targets(
                contacted_agents if agent_welcome_enabled else [],
                normalized_directives,
            )
            explicit_followup_targets: list[str] = []
            queued_target_counts: dict[str, int] = {}
            queued_targeted_followup = False
            for directive in normalized_directives:
                directive_service_id = str(directive.get("service_id") or "").strip()
                directive_state = str(directive.get("audit_state") or "all_clear").strip().lower()
                directive_feedback_xml = str(directive.get("continue_xml") or "").strip()
                if (
                    directive_state == "needs_compact"
                    and resolved_audit_state == "all_clear"
                    and bool(directive.get("request_compact", False))
                ):
                    directive_state = "all_clear"
                    if not directive_feedback_xml:
                        directive_summary = str(directive.get("summary") or "").strip()
                        directive_reason = str(directive.get("request_compact_reason") or "").strip()
                        directive_feedback_xml = (
                            "<aize_goal_feedback><summary>"
                            + html.escape(
                                directive_summary
                                or directive_reason
                                or "Compaction completed; continue the requested work."
                            )
                            + "</summary></aize_goal_feedback>"
                        )
                if (
                    directive_service_id
                    and directive_state == "all_clear"
                    and directive_feedback_xml
                ):
                    is_ws_peer_target = directive_service_id.startswith("ws-peer-")
                    if is_ws_peer_target:
                        # WS peers do not have a local adapter process to receive router
                        # dispatch_pending. Transport their goal feedback through the
                        # subscribed session history stream instead.
                        queued_target_counts[directive_service_id] = 1
                    else:
                        pending_for_service = append_service_pending_input(
                            runtime_root,
                            service_id=directive_service_id,
                            agent_id=resolve_session_agent_id(
                                runtime_root,
                                username=username,
                                session_id=session_id,
                                service_id=directive_service_id,
                            ),
                            username=username,
                            session_id=session_id,
                            entry=make_aize_pending_input(
                                kind="goal_feedback",
                                role="system",
                                text=directive_feedback_xml,
                            ),
                        )
                        queued_target_counts[directive_service_id] = len(pending_for_service)
                    if directive_service_id not in explicit_followup_targets:
                        explicit_followup_targets.append(directive_service_id)
                    queued_targeted_followup = True
                    _feedback_summary = str(audit.get("summary") or "").strip() if audit is not None else ""
                    feedback_history_entry = {
                        "direction": "session_input",
                        "kind": "goal_feedback",
                        "ts": utc_ts(),
                        "service_id": directive_service_id,
                        "to": directive_service_id,
                        "text": _feedback_summary or "GoalManager requested more work",
                    }
                    if is_ws_peer_target:
                        feedback_history_entry["pending_input_text"] = directive_feedback_xml
                    goal_history_sink(feedback_history_entry)
            if explicit_followup_targets:
                dispatch_targets = goal_followup_dispatch_targets(
                    [{"service_id": item} for item in explicit_followup_targets],
                    normalized_directives,
                )
            for dispatch_service_id in dispatch_targets:
                pending_for_target_count = queued_target_counts.get(dispatch_service_id)
                if pending_for_target_count is None:
                    pending_for_target_count = len(
                        load_service_pending_inputs(
                            runtime_root,
                            service_id=dispatch_service_id,
                            agent_id=resolve_session_agent_id(
                                runtime_root,
                                username=username,
                                session_id=session_id,
                                service_id=dispatch_service_id,
                            ),
                            username=username,
                            session_id=session_id,
                        )
                    )
                write_jsonl(
                    log_path,
                    {
                        "type": "service.goal_audit_dispatch_check",
                        "ts": utc_ts(),
                        "service_id": goal_manager_service_id,
                        "process_id": process_id,
                        "scope": {"username": username, "session_id": session_id},
                        "dispatch_target": dispatch_service_id,
                        "pending_for_target_count": pending_for_target_count,
                        "queued_targeted_followup": queued_targeted_followup,
                        "will_skip": bool(not pending_for_target_count and queued_targeted_followup),
                    },
                )
                if not pending_for_target_count and queued_targeted_followup:
                    continue
                if dispatch_service_id.startswith("ws-peer-"):
                    write_jsonl(
                        log_path,
                        {
                            "type": "service.goal_audit_ws_peer_dispatch",
                            "ts": utc_ts(),
                            "service_id": goal_manager_service_id,
                            "process_id": process_id,
                            "scope": {"username": username, "session_id": session_id},
                            "dispatch_target": dispatch_service_id,
                            "transport": "history_subscriber",
                        },
                    )
                    continue
                _ensure_dispatch_allowed_peer(
                    runtime_root,
                    from_service_id=goal_manager_service_id,
                    to_service_id=dispatch_service_id,
                )
                send_tx(
                    make_dispatch_pending_message(
                        manifest=manifest,
                        from_service_id=goal_manager_service_id,
                        to_service_id=dispatch_service_id,
                        process_id=process_id,
                        run_id=f"goal-audit-{uuid.uuid4().hex[:8]}",
                        username=username,
                        session_id=session_id,
                        auth_context=None,
                        reason="goal_feedback",
                        session_agent_id=resolve_session_agent_id(
                            runtime_root,
                            username=username,
                            session_id=session_id,
                            service_id=dispatch_service_id,
                        ),
                    )
                )
        except Exception as exc:
            previous_session = get_session_settings(
                runtime_root,
                username=username,
                session_id=session_id,
            )
            save_agent_audit_state(
                runtime_root,
                service_id=goal_manager_service_id,
                username=username,
                session_id=session_id,
                audit_state="panic",
            )
            updated_session = update_session_goal_flags(
                runtime_root,
                username=username,
                session_id=session_id,
                goal_completed=False,
                goal_progress_state="in_progress",
            )
            emit_goal_status(updated_session, previous_session)
            persist_goal_audit_failure(
                runtime_root=runtime_root,
                log_path=log_path,
                service_id=goal_manager_service_id,
                process_id=process_id,
                goal_audit_job_id=goal_audit_job_id,
                username=username,
                session_id=session_id,
                error=repr(exc),
                history_sink=goal_history_sink,
            )
            maybe_spawn_failure_recovery(
                username=username,
                session_id=session_id,
                failure_event={
                    "type": "service.goal_manager_compact_failed",
                    "error": repr(exc),
                    "service_id": goal_manager_service_id,
                    "process_id": process_id,
                    "goal_audit_job_id": goal_audit_job_id,
                },
                failure_service_id=goal_manager_service_id,
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
        provider_session_slot = _dispatch_provider_session_slot(message)
        dispatch_reason = _dispatch_reason(message)
        service_pending_only = _dispatch_reason_uses_service_pending_only(dispatch_reason)

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

        def emit_scoped_goal_status(
            updated_session: dict[str, Any] | None,
            previous_session: dict[str, Any] | None,
        ) -> None:
            if not (scope_username and scope_session_id) or not updated_session:
                return
            publish_goal_status_changed(
                lambda history_username, history_session_id, entry: append_scoped_history(
                    entry,
                    limit=GOAL_AUDIT_HISTORY_LIMIT,
                ),
                service_id=service_id,
                username=scope_username,
                session_id=scope_session_id,
                session=updated_session,
                previous_session=previous_session,
            )
        if dispatch_pending:
            if not (scope_username and scope_session_id):
                return
            target_agent_id = _dispatch_target_agent_id(
                message_meta_get(message, "session_agent_id"),
                runtime_root=runtime_root,
                username=scope_username,
                session_id=scope_session_id,
                service_id=service_id,
                provider_session_slot=provider_session_slot,
            )
            # Quick pre-check (peek, not drain) to skip lock contention for obvious noops
            session_pending_inputs = [] if service_pending_only else load_pending_inputs(
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
            goal_manager_pending_inputs = []
            if dispatch_reason == "goal_manager_review":
                goal_manager_pending_inputs = _load_or_repair_goal_manager_pending_inputs(
                    runtime_root,
                    username=scope_username,
                    session_id=scope_session_id,
                )
            if not (session_pending_inputs or service_pending_inputs or goal_manager_pending_inputs):
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
            dispatch_session_settings = get_session_settings(
                runtime_root,
                username=scope_username,
                session_id=scope_session_id,
            ) or {}
            pending_inputs_preview = (
                list(session_pending_inputs)
                + list(service_pending_inputs)
                + list(goal_manager_pending_inputs)
            )
            if _should_defer_dispatch_for_completed_goal(
                session_settings=dispatch_session_settings,
                pending_inputs=pending_inputs_preview,
            ):
                write_jsonl(
                    log_path,
                    {
                        "type": "service.dispatch_pending_goal_complete_deferred",
                        "ts": utc_ts(),
                        "service_id": service_id,
                        "process_id": process_id,
                        "reply_index": reply_index,
                        "scope": {"username": scope_username, "session_id": scope_session_id},
                        "queued_input_count": len(pending_inputs_preview),
                    },
                )
                return
        try:
            lock_session_id = scope_session_id
            if provider_session_slot in {"interactive_agent", "worker_agent"}:
                lock_session_id = f"{scope_session_id}::{provider_session_slot}"
            with scope_lock_for(scope_username, lock_session_id):
                goal_manager_review_items: list[dict[str, Any]] = []
                goal_child_session_request_items: list[dict[str, Any]] = []
                # Drain pending inputs inside the scope lock to prevent a race where two
                # dispatch_pending messages both see a non-empty queue and each launch Codex.
                if dispatch_pending:
                    dispatch_session_settings = get_session_settings(
                        runtime_root,
                        username=scope_username,
                        session_id=scope_session_id,
                    ) or {}
                    session_pending_inputs = [] if service_pending_only else load_pending_inputs(
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
                    pending_inputs_preview = list(session_pending_inputs) + list(service_pending_inputs)
                    if _should_defer_dispatch_for_completed_goal(
                        session_settings=dispatch_session_settings,
                        pending_inputs=pending_inputs_preview,
                    ):
                        write_jsonl(
                            log_path,
                            {
                                "type": "service.dispatch_pending_goal_complete_deferred",
                                "ts": utc_ts(),
                                "service_id": service_id,
                                "process_id": process_id,
                                "reply_index": reply_index,
                                "scope": {"username": scope_username, "session_id": scope_session_id},
                                "queued_input_count": len(pending_inputs_preview),
                            },
                        )
                        return
                    pending_inputs = [] if service_pending_only else drain_pending_inputs(
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
                    if dispatch_reason == "goal_manager_review":
                        _load_or_repair_goal_manager_pending_inputs(
                            runtime_root,
                            username=scope_username,
                            session_id=scope_session_id,
                        )
                        for gm_pending_item in drain_goal_manager_pending_inputs(
                            runtime_root,
                            username=scope_username,
                            session_id=scope_session_id,
                        ):
                            if str((gm_pending_item or {}).get("kind") or "").strip().lower() == "goal_manager_review":
                                service_pending_inputs.append(gm_pending_item)
                            else:
                                service_pending_inputs.append(
                                    make_aize_pending_input(
                                        kind="goal_manager_review",
                                        role="system",
                                        text=json.dumps(gm_pending_item, ensure_ascii=False),
                                    )
                                )
                    goal_manager_review_items = decode_goal_manager_review_inputs(service_pending_inputs)
                    goal_child_session_request_items = decode_goal_child_session_request_inputs(service_pending_inputs)
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
                    if provider_session_slot == "interactive_agent":
                        latest_user_dialogue = next(
                            (
                                item
                                for item in reversed(pending_inputs)
                                if str(item.get("kind") or "").strip().lower()
                                in {"user_dialogue", "user_message", "interactive_worker_result"}
                            ),
                            pending_inputs[-1],
                        )
                        incoming_text = str(latest_user_dialogue.get("text") or "").strip()
                    else:
                        batch_instruction = (
                            "Respond to the queued talk inputs in order, prioritizing the latest user-visible requirement while preserving relevant pending system context."
                        )
                        if batch_has_input_kind(pending_inputs, "restart_resume") or batch_has_input_kind(pending_inputs, "scheduled_resume"):
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

                if dispatch_pending and goal_manager_review_items and scope_username and scope_session_id:
                    for child_signal in goal_child_session_request_items:
                        append_service_pending_input(
                            runtime_root,
                            service_id=service_id,
                            agent_id=target_agent_id,
                            username=scope_username,
                            session_id=scope_session_id,
                            entry=make_aize_pending_input(
                                kind="goal_child_session_request",
                                role="system",
                                text=json.dumps(child_signal, ensure_ascii=False),
                            ),
                        )
                    goal_review_settings = get_session_settings(
                        runtime_root,
                        username=scope_username,
                        session_id=scope_session_id,
                    ) or {}
                    run_goal_manager_review(
                        username=scope_username,
                        session_id=scope_session_id,
                        session_settings=goal_review_settings,
                        goal_text=str(goal_review_settings.get("goal_text") or "").strip(),
                        reply_index=reply_index,
                        goal_manager_service_id=service_id,
                        gm_work_items=goal_manager_review_items,
                        append_scoped_history=append_scoped_history,
                    )
                    return

                if dispatch_pending and goal_child_session_request_items and scope_username and scope_session_id:
                    latest_signal = goal_child_session_request_items[-1]
                    child_requests = [
                        dict(item.get("request") or {})
                        for item in goal_child_session_request_items
                        if isinstance(item, dict) and isinstance(item.get("request"), dict)
                    ]
                    if child_requests:
                        _materialize_goal_child_sessions(
                            runtime_root=runtime_root,
                            username=scope_username,
                            session_id=scope_session_id,
                            goal_id=str(latest_signal.get("goal_id") or "").strip(),
                            goal_text=str(latest_signal.get("goal_text") or "").strip(),
                            goal_manager_service_id=service_id,
                            child_goal_requests=child_requests,
                            dispatch_child_session=lambda child_session_id: (
                                kickoff_goal_child_session_for_dispatch(
                                    username=scope_username,
                                    parent_session_id=scope_session_id,
                                    child_session_id=child_session_id,
                                    goal_manager_service_id=service_id,
                                )
                            ),
                        )
                    return

                if provider_session_slot == "interactive_agent":
                    recent_context = (
                        _interactive_recent_context(
                            get_user_history(
                                runtime_root,
                                username=scope_username,
                                session_id=scope_session_id,
                            )
                        )
                        if scope_username and scope_session_id
                        else []
                    )
                    prompt = build_interactive_prompt(
                        text=incoming_text,
                        username=scope_username or "",
                        session_id=scope_session_id or "",
                        recent_context=recent_context,
                    )
                else:
                    prompt_text = incoming_text
                    if provider_session_slot == "worker_agent" and scope_username and scope_session_id:
                        session_skills_block = _worker_session_skills_block(
                            runtime_root,
                            username=scope_username,
                            session_id=scope_session_id,
                        )
                        if session_skills_block:
                            prompt_text = f"{session_skills_block}\n\n{incoming_text}"
                    prompt = build_prompt(self_service, peer_service, prompt_text, reply_index)
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
                        "session_slot": provider_session_slot,
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
                    join_session_agent(
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
                        role="agent",
                        transport="local_dispatch",
                    )

                profile_ephemeral = False

                def emit_provider_event(event: dict[str, Any]) -> None:
                    write_jsonl(
                        log_path,
                        {
                            "type": "service.event",
                            "ts": utc_ts(),
                            "service_id": service_id,
                            "session_slot": provider_session_slot,
                            "process_id": process_id,
                            "run_id": message_meta_get(message, "run_id"),
                            "event": event,
                        },
                    )
                    if (
                        self_service["kind"] == "codex"
                        and event.get("type") == "thread.started"
                        and not profile_ephemeral
                    ):
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
                    if provider_session_slot == "interactive_agent" and _provider_event_has_user_visible_agent_text(event):
                        return
                    if dispatch_reason == "interactive_worker_request" and provider_session_slot == "worker_agent":
                        return
                    if provider_session_slot == "worker_agent" and _provider_event_has_user_visible_agent_text(event):
                        session_settings = (
                            get_session_settings(
                                runtime_root,
                                username=scope_username,
                                session_id=scope_session_id,
                            )
                            if scope_username and scope_session_id
                            else {}
                        ) or {}
                        if bool(session_settings.get("communication_agent_enabled", False)):
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
                    agent_profile = message_meta_get(message, "agent_profile")
                    profile_model = ""
                    profile_config: dict[str, Any] = {}
                    provider_session_slot = _dispatch_provider_session_slot(
                        message,
                        agent_profile if isinstance(agent_profile, dict) else None,
                    )
                    if isinstance(agent_profile, dict):
                        profile_model = str(agent_profile.get("model") or "").strip()
                        profile_ephemeral = bool(agent_profile.get("ephemeral")) or (
                            str(agent_profile.get("session_mode") or "").strip().lower() == "ephemeral"
                        )
                        raw_profile_config = agent_profile.get("config") or agent_profile.get("config_overrides")
                        if isinstance(raw_profile_config, dict):
                            profile_config = {
                                str(key).strip(): value
                                for key, value in raw_profile_config.items()
                                if str(key).strip()
                            }
                    try:
                        process_record = get_process_record(runtime_root, process_id)
                    except KeyError:
                        process_record = {}
                    scoped_session_id = load_codex_session(
                        runtime_root,
                        service_id=service_id,
                        username=scope_username,
                        session_id=scope_session_id,
                        slot=provider_session_slot,
                    )
                    if scope_username and scope_session_id:
                        session_id = scoped_session_id
                    else:
                        session_id = scoped_session_id or process_record.get("codex_session_id")
                    if provider_session_slot == "interactive_agent" and scope_username and scope_session_id:
                        worker_session_id = load_codex_session(
                            runtime_root,
                            service_id=service_id,
                            username=scope_username,
                            session_id=scope_session_id,
                            slot="worker_agent",
                        )
                        if worker_session_id and worker_session_id == session_id:
                            session_id = None
                    if profile_ephemeral:
                        session_id = None

                    final_text, provider_events, next_session_id = run_codex(
                        prompt,
                        session_id=session_id,
                        response_schema_id=self_service.get("response_schema_id"),
                        model=profile_model or str((self_service.get("config") or {}).get("model") or "").strip() or None,
                        config_overrides=profile_config,
                        ephemeral=profile_ephemeral,
                        on_event=emit_provider_event,
                    )
                    if not profile_ephemeral:
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
                            slot=provider_session_slot,
                        )
                elif self_service["kind"] == "claude":
                    scoped_claude_session_id = load_claude_session(
                        runtime_root,
                        service_id=service_id,
                        username=scope_username,
                        session_id=scope_session_id,
                        slot=provider_session_slot,
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
                        slot=provider_session_slot,
                    )
                elif self_service["kind"] == "gemini":
                    scoped_gemini_session_id = load_gemini_session(
                        runtime_root,
                        service_id=service_id,
                        username=scope_username,
                        session_id=scope_session_id,
                        slot=provider_session_slot,
                    )
                    final_text, provider_events, next_session_id = run_gemini(
                        prompt,
                        session_id=scoped_gemini_session_id,
                        response_schema_id=self_service.get("response_schema_id"),
                        model=str((self_service.get("config") or {}).get("model") or "").strip() or None,
                        on_event=emit_provider_event,
                    )
                    save_gemini_session(
                        runtime_root,
                        service_id=service_id,
                        provider_session_id=next_session_id,
                        username=scope_username,
                        session_id=scope_session_id,
                        slot=provider_session_slot,
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
            response_schema_id = (
                "service_control_v1"
                if provider_session_slot == "interactive_agent"
                else self_service.get("response_schema_id")
            )
            visible_text, spawn_requests, schema_error = parse_service_response_with_fallback(
                final_text,
                response_schema_id,
            )
            visible_text, user_response_wait = _extract_user_response_wait_control(visible_text)
            if dispatch_reason == "interactive_worker_result" and not visible_text:
                visible_text = _interactive_worker_result_fallback_text(incoming_text)
            if scope_username and scope_session_id:
                if isinstance(user_response_wait, dict):
                    recorded_user_response_request = record_session_user_response_request(
                        runtime_root,
                        username=scope_username,
                        session_id=scope_session_id,
                        status="recorded",
                        timeout_seconds=int(user_response_wait.get("timeout_seconds", 300) or 300),
                        prompt_text=visible_text,
                        request_id=user_response_wait.get("request_id"),
                        request_reason=user_response_wait.get("request_reason") or "agent_not_authorized",
                        source_service_id=service_id,
                        requested_by_role=provider_session_slot or "agent",
                    )
                    recorded_request_id = str(
                        (recorded_user_response_request or {}).get("user_response_wait_request_id")
                        or user_response_wait.get("request_id")
                        or ""
                    ).strip()
                    recorded_timeout_seconds = int(
                        (recorded_user_response_request or {}).get("user_response_wait_timeout_seconds")
                        or user_response_wait.get("timeout_seconds", 300)
                        or 300
                    )
                    recorded_effective_timeout_seconds = int(
                        (recorded_user_response_request or {}).get("user_response_wait_effective_timeout_seconds")
                        or recorded_timeout_seconds
                    )
                    append_user_history(
                        runtime_root,
                        username=scope_username,
                        session_id=scope_session_id,
                        entry={
                            "direction": "event",
                            "ts": utc_ts(),
                            "service_id": service_id,
                            "event_type": "service.user_response_wait_ignored",
                            "text": "Agent attempted to request a user reply, but only GoalManager may start a user response wait.",
                            "event": {
                                "type": "service.user_response_wait_ignored",
                                "request_id": recorded_request_id,
                                "generated_at": str(
                                    (recorded_user_response_request or {}).get("user_response_wait_generated_at") or ""
                                ),
                                "timeout_seconds": recorded_timeout_seconds,
                                "effective_timeout_seconds": recorded_effective_timeout_seconds,
                                "until_at": str(
                                    (recorded_user_response_request or {}).get("user_response_wait_until_at") or ""
                                ),
                                "prompt_text": visible_text,
                                "source_service_id": service_id,
                                "requested_by_role": provider_session_slot or "agent",
                                "reason": "agent_not_authorized",
                            },
                        },
                        limit=GOAL_AUDIT_HISTORY_LIMIT,
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
                    "user_response_wait_active": bool(user_response_wait),
                    "schema_error": schema_error,
                },
            )
            if schema_error:
                append_user_history(
                    runtime_root,
                    username=scope_username,
                    session_id=scope_session_id,
                    entry={
                        "direction": "event",
                        "ts": utc_ts(),
                        "service_id": service_id,
                        "event_type": "service.response_schema_fallback",
                        "text": "Agent reply used plain-text fallback because schema parsing failed.",
                        "event": {
                            "type": "service.response_schema_fallback",
                            "error": schema_error,
                        },
                    },
                    limit=GOAL_AUDIT_HISTORY_LIMIT,
                )
            if (
                dispatch_reason == "interactive_worker_request"
                and provider_session_slot == "worker_agent"
                and scope_username
                and scope_session_id
            ):
                request_item = next(
                    (
                        item
                        for item in pending_inputs
                        if str(item.get("kind") or "").strip().lower() == "interactive_worker_request"
                    ),
                    {},
                )
                request_id = str(request_item.get("request_id") or uuid.uuid4().hex[:12])
                source_user_text = str(request_item.get("source_user_text") or "").strip()
                worker_result_text = visible_text or final_text
                target_interactive_service_id, interactive_agent_id = _interactive_worker_resume_target(
                    request_item,
                    fallback_service_id=service_id,
                    session_id=scope_session_id,
                )
                resume_text = _interactive_resume_xml(
                    request_id=request_id,
                    worker_text=worker_result_text,
                    source_user_text=source_user_text,
                )
                resume_entry = make_aize_pending_input(
                    kind="interactive_worker_result",
                    role="system",
                    text=resume_text,
                )
                resume_entry["request_id"] = request_id
                append_service_pending_input(
                    runtime_root,
                    service_id=target_interactive_service_id,
                    agent_id=interactive_agent_id,
                    username=scope_username,
                    session_id=scope_session_id,
                    entry=resume_entry,
                )
                session_settings = get_session_settings(
                    runtime_root,
                    username=scope_username,
                    session_id=scope_session_id,
                ) or {}
                interactive_priority = active_agent_profile_priority(
                    session_settings.get("communication_agent_priority")
                )
                interactive_profile = dict(interactive_priority[0]) if interactive_priority else {
                    "provider": str(self_service.get("kind") or "codex"),
                    "session_slot": "interactive_agent",
                    "session_mode": "ephemeral",
                    "ephemeral": True,
                }
                interactive_profile["session_slot"] = "interactive_agent"
                _ensure_dispatch_allowed_peer(
                    runtime_root,
                    from_service_id=service_id,
                    to_service_id=target_interactive_service_id,
                )
                send_tx(
                    make_dispatch_pending_message(
                        manifest=manifest,
                        from_service_id=service_id,
                        to_service_id=target_interactive_service_id,
                        process_id=process_id,
                        run_id=message_meta_get(message, "run_id"),
                        username=scope_username,
                        session_id=scope_session_id,
                        auth_context=message_meta_get(message, "auth")
                        if isinstance(message_meta_get(message, "auth"), dict)
                        else None,
                        reason="interactive_worker_result",
                        reply_to_service_id=sender_service_id,
                        session_agent_id=interactive_agent_id,
                        agent_profile=interactive_profile,
                    )
                )
                append_user_history(
                    runtime_root,
                    username=scope_username,
                    session_id=scope_session_id,
                    entry={
                        "direction": "event",
                        "ts": utc_ts(),
                        "service_id": service_id,
                        "event_type": "interactive.worker_completed",
                        "text": "WorkerAgent completed background investigation and resumed InteractiveAgent.",
                        "event": {
                            "type": "interactive.worker_completed",
                            "request_id": request_id,
                            "worker_service_id": service_id,
                            "interactive_service_id": target_interactive_service_id,
                            "interactive_agent_id": interactive_agent_id,
                            "worker_result_text": worker_result_text,
                        },
                    },
                    limit=GOAL_AUDIT_HISTORY_LIMIT,
                )
                if (
                    session_has_active_in_progress_goal(session_settings)
                    and not bool(session_settings.get("user_response_wait_active", False))
                ):
                    goal_manager_service_id = resolve_goal_manager_dispatch_service(
                        username=scope_username,
                        session_id=scope_session_id,
                    )
                    if goal_manager_service_id:
                        append_goal_manager_pending_input(
                            runtime_root,
                            username=scope_username,
                            session_id=scope_session_id,
                            entry=make_aize_pending_input(
                                kind="goal_manager_review",
                                role="system",
                                text=json.dumps(
                                    {
                                        "kind": "interactive_worker_result",
                                        "request_id": request_id,
                                        "worker_service_id": service_id,
                                        "interactive_service_id": target_interactive_service_id,
                                        "source_user_text": source_user_text,
                                        "worker_result_text": worker_result_text,
                                    },
                                    ensure_ascii=False,
                                ),
                            ),
                        )
                        _ensure_dispatch_allowed_peer(
                            runtime_root,
                            from_service_id=service_id,
                            to_service_id=goal_manager_service_id,
                        )
                        send_tx(
                            make_dispatch_pending_message(
                                manifest=manifest,
                                from_service_id=service_id,
                                to_service_id=goal_manager_service_id,
                                process_id=process_id,
                                run_id=f"interactive-worker-goal-review-{uuid.uuid4().hex[:8]}",
                                username=scope_username,
                                session_id=scope_session_id,
                                auth_context=message_meta_get(message, "auth")
                                if isinstance(message_meta_get(message, "auth"), dict)
                                else None,
                                reason="goal_manager_review",
                                reply_to_service_id=sender_service_id,
                                session_agent_id=resolve_session_agent_id(
                                    runtime_root,
                                    username=scope_username,
                                    session_id=scope_session_id,
                                    service_id=goal_manager_service_id,
                                ),
                            )
                        )
                visible_text = ""
            for control in spawn_requests:
                incoming_auth = message_meta_get(message, "auth")
                incoming_auth_context = dict(incoming_auth) if isinstance(incoming_auth, dict) else None
                handed_off_children: list[dict[str, str]] = []
                spawn_scope_session: dict[str, Any] | None = None
                force_spawn_child_handoff = False
                if scope_username and scope_session_id:
                    spawn_scope_session = get_session_settings(
                        runtime_root,
                        username=scope_username,
                        session_id=scope_session_id,
                    )
                    force_spawn_child_handoff = _should_force_spawn_request_child_handoff(
                        spawn_scope_session
                    )
                if scope_username and scope_session_id:
                    handed_off_children = _route_spawn_request_to_communication_child_session(
                        runtime_root=runtime_root,
                        username=scope_username,
                        session_id=scope_session_id,
                        goal_manager_service_id=service_id,
                        control=control,
                        dispatch_child_session=lambda child_session_id: (
                            kickoff_goal_child_session_for_dispatch(
                                username=scope_username,
                                parent_session_id=scope_session_id,
                                child_session_id=child_session_id,
                                goal_manager_service_id=service_id,
                            )
                        ),
                    )
                    if handed_off_children:
                        append_scoped_history(
                            {
                                "direction": "event",
                                "ts": utc_ts(),
                                "service_id": service_id,
                                "event_type": "service.spawn_request_handed_off_to_child_session",
                                "text": f"Delegation request was handed off to {len(handed_off_children)} child session(s).",
                                "event": {
                                    "type": "service.spawn_request_handed_off_to_child_session",
                                    "requested_service_id": str(control.get("service", {}).get("service_id") or "").strip(),
                                    "children": handed_off_children,
                                },
                            },
                            limit=GOAL_AUDIT_HISTORY_LIMIT,
                        )
                        continue
                if (
                    not handed_off_children
                    and scope_username
                    and scope_session_id
                    and (
                        force_spawn_child_handoff
                        or not _service_can_spawn_children(
                            self_service=self_service,
                            auth_context=incoming_auth_context,
                        )
                    )
                ):
                    handed_off_children = _handoff_spawn_request_to_child_session(
                        runtime_root=runtime_root,
                        username=scope_username,
                        session_id=scope_session_id,
                        goal_manager_service_id=service_id,
                        control=control,
                        dispatch_child_session=lambda child_session_id: (
                            kickoff_goal_child_session_for_dispatch(
                                username=scope_username,
                                parent_session_id=scope_session_id,
                                child_session_id=child_session_id,
                                goal_manager_service_id=service_id,
                            )
                        ),
                    )
                    if handed_off_children:
                        append_scoped_history(
                            {
                                "direction": "event",
                                "ts": utc_ts(),
                                "service_id": service_id,
                                "event_type": "service.spawn_request_handed_off_to_child_session",
                                "text": f"Delegation request was handed off to {len(handed_off_children)} child session(s).",
                                "event": {
                                    "type": "service.spawn_request_handed_off_to_child_session",
                                    "requested_service_id": str(control.get("service", {}).get("service_id") or "").strip(),
                                    "children": handed_off_children,
                                },
                            },
                            limit=GOAL_AUDIT_HISTORY_LIMIT,
                        )
                        continue
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
                reply_to_service_id = message_meta_get(message, "reply_to_service_id")
                if isinstance(reply_to_service_id, str) and reply_to_service_id.strip():
                    message_set_meta(
                        spawn_message,
                        "reply_to_service_id",
                        reply_to_service_id.strip(),
                    )
                if incoming_auth_context is not None:
                    message_set_meta(spawn_message, "auth", incoming_auth_context)
                send_tx(spawn_message)
                initial_prompt = control.get("initial_prompt")
                if initial_prompt:
                    child_id = control["service"]["service_id"]
                    prompt_ready, prompt_route_reason = _await_spawn_initial_prompt_route(
                        runtime_root,
                        sender_service_id=service_id,
                        child_service_id=child_id,
                    )
                    if not prompt_ready:
                        write_jsonl(
                            log_path,
                            {
                                "type": "service.spawn_initial_prompt_skipped",
                                "ts": utc_ts(),
                                "service_id": service_id,
                                "process_id": process_id,
                                "child_service_id": child_id,
                                "reason": prompt_route_reason,
                            },
                        )
                        continue
                    if _dispatch_spawn_initial_prompt(
                        runtime_root=runtime_root,
                        manifest=manifest,
                        process_id=process_id,
                        service_id=service_id,
                        child_service_id=child_id,
                        initial_prompt=str(initial_prompt),
                        run_id=str(message_meta_get(message, "run_id") or ""),
                        send_tx=send_tx,
                        auth_context=incoming_auth_context,
                        scope_username=scope_username,
                        scope_session_id=scope_session_id,
                    ):
                        continue
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
                        auth_context=incoming_auth_context,
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
            if provider_session_slot == "interactive_agent":
                if scope_username and scope_session_id:
                    session_settings = (
                        get_session_settings(runtime_root, username=scope_username, session_id=scope_session_id) or {}
                    )
                    if _should_complete_communication_goal_after_reply(
                        session_settings,
                        visible_text=visible_text,
                    ):
                        sync_communication_goal_progress(
                            runtime_root,
                            username=scope_username,
                            session_id=scope_session_id,
                            completed=True,
                            session=session_settings,
                        )
                write_jsonl(
                    log_path,
                    {
                        "type": "service.interactive_post_turn_skipped",
                        "ts": utc_ts(),
                        "service_id": service_id,
                        "process_id": process_id,
                        "scope": {"username": scope_username, "session_id": scope_session_id},
                        "reason": "interactive_agent_direct_reply",
                    },
                )
                return

            if scope_username and scope_session_id:
                    try:
                        turn_completed_at = utc_ts()
                        session_settings = (
                            get_session_settings(runtime_root, username=scope_username, session_id=scope_session_id) or {}
                        )
                        communication_agent_enabled = bool(
                            session_settings.get("communication_agent_enabled", False)
                        )
                        actionable_input_present = _actionable_post_turn_input_present(
                            incoming_text,
                            communication_agent_enabled=communication_agent_enabled,
                        )
                        append_session_turn_completed_input = _should_append_session_turn_completed_input(
                            communication_agent_enabled=communication_agent_enabled,
                            actionable_input_present=actionable_input_present,
                            spawn_request_count=len(spawn_requests),
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
                        if append_session_turn_completed_input:
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
                                            f"  <completed_at>{html.escape(turn_completed_at)}</completed_at>",
                                            f"  <session_dir>{html.escape(str(scope_session_dir))}</session_dir>",
                                            f"  <timeline_path>{html.escape(str(scope_timeline))}</timeline_path>",
                                            "  <history_instruction>Read the session files directly for the completed reply and related events instead of relying on inline event text.</history_instruction>",
                                            "</aize_turn_completed>",
                                        ]
                                    ),
                                ),
                            )
                        join_session_agent(
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
                            role="agent",
                            transport="local_dispatch",
                            turn_completed_at=turn_completed_at,
                        )
                        write_jsonl(
                            log_path,
                            {
                                "type": "service.post_turn_turn_completed_appended",
                                "ts": utc_ts(),
                                "service_id": service_id,
                                "process_id": process_id,
                                "scope": {"username": scope_username, "session_id": scope_session_id},
                                "appended": append_session_turn_completed_input,
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
                        session_settings = (
                            get_session_settings(
                                runtime_root,
                                username=scope_username,
                                session_id=scope_session_id,
                            )
                            or session_settings
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
                            elif self_service["kind"] == "codex":
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
                            elif self_service["kind"] == "gemini":
                                maybe_auto_compact_gemini_session(
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
                                    updated_session = update_session_goal_flags(
                                        runtime_root,
                                        username=scope_username,
                                        session_id=scope_session_id,
                                        goal_completed=False,
                                        goal_progress_state="in_progress",
                                    )
                                    emit_scoped_goal_status(updated_session, latest_session_settings)
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
                            or batch_has_input_kind(incoming_text, "scheduled_resume")
                        )
                        goal_should_continue = _should_enqueue_post_turn_goal_manager_followup(
                            provider_session_slot=provider_session_slot,
                            turn_completed_input_present=turn_completed_input_present,
                            goal_input_present=goal_input_present,
                            actionable_input_present=actionable_input_present,
                            goal_text=goal_text,
                            goal_active=goal_active,
                            goal_completed=goal_completed,
                            goal_progress_state=goal_progress_state,
                            goal_audit_state=goal_audit_state,
                            user_response_wait_active=bool(
                                session_settings.get("user_response_wait_active", False)
                            ),
                            communication_agent_enabled=communication_agent_enabled,
                            visible_text_present=bool(visible_text),
                            spawn_request_count=len(spawn_requests),
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
                                "actionable_input_present": actionable_input_present,
                                "goal_should_continue": goal_should_continue,
                            },
                        )
                        advance_review_cursor_without_followup = (
                            _should_advance_goal_manager_review_cursor_without_followup(
                                communication_agent_enabled=communication_agent_enabled,
                                actionable_input_present=actionable_input_present,
                                spawn_request_count=len(spawn_requests),
                                goal_should_continue=goal_should_continue,
                            )
                        )
                        if advance_review_cursor_without_followup:
                            update_goal_manager_review_cursor(
                                runtime_root,
                                username=scope_username,
                                session_id=scope_session_id,
                                last_turn_completed_at=turn_completed_at,
                            )
                            write_jsonl(
                                log_path,
                                {
                                    "type": "service.post_turn_review_cursor_advanced",
                                    "ts": utc_ts(),
                                    "service_id": service_id,
                                    "process_id": process_id,
                                    "scope": {"username": scope_username, "session_id": scope_session_id},
                                    "last_turn_completed_at": turn_completed_at,
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
                                    goal_manager_service_id = resolve_goal_manager_dispatch_service(
                                        username=scope_username,
                                        session_id=scope_session_id,
                                    ) or service_id
                                    gm_work_items = drain_goal_manager_pending_inputs(
                                        runtime_root,
                                        username=scope_username,
                                        session_id=scope_session_id,
                                    )
                                    if not gm_work_items:
                                        return
                                    queued_review = []
                                    for gm_work_item in gm_work_items:
                                        queued_review = append_service_pending_input(
                                            runtime_root,
                                            service_id=goal_manager_service_id,
                                            agent_id=resolve_session_agent_id(
                                                runtime_root,
                                                username=scope_username,
                                                session_id=scope_session_id,
                                                service_id=goal_manager_service_id,
                                            ),
                                            username=scope_username,
                                            session_id=scope_session_id,
                                            entry=make_aize_pending_input(
                                                kind="goal_manager_review",
                                                role="system",
                                                text=json.dumps(gm_work_item, ensure_ascii=False),
                                            ),
                                        )
                                    goal_manager_state_path = session_goal_manager_state_path(
                                        runtime_root,
                                        username=scope_username,
                                        session_id=scope_session_id,
                                    )
                                    goal_manager_state = read_json_file(goal_manager_state_path) or {}
                                    goal_manager_state.update(
                                        {
                                            "state": "queued",
                                            "service_id": goal_manager_service_id,
                                            "pending_work_items": gm_work_items,
                                            "last_queued_turn_completed_at": _latest_goal_manager_turn_completed_ts(
                                                gm_work_items
                                            ),
                                            "updated_at": utc_ts(),
                                        }
                                    )
                                    write_json_file(goal_manager_state_path, goal_manager_state)
                                    _ensure_dispatch_allowed_peer(
                                        runtime_root,
                                        from_service_id=service_id,
                                        to_service_id=goal_manager_service_id,
                                    )
                                    send_tx(
                                        make_dispatch_pending_message(
                                            manifest=manifest,
                                            from_service_id=service_id,
                                            to_service_id=goal_manager_service_id,
                                            process_id=process_id,
                                            run_id=f"goal-manager-review-{uuid.uuid4().hex[:8]}",
                                            username=scope_username,
                                            session_id=scope_session_id,
                                            auth_context=None,
                                            reason="goal_manager_review",
                                            session_agent_id=resolve_session_agent_id(
                                                runtime_root,
                                                username=scope_username,
                                                session_id=scope_session_id,
                                                service_id=goal_manager_service_id,
                                            ),
                                        )
                                    )
                                    write_jsonl(
                                        log_path,
                                        {
                                            "type": "service.goal_manager_review_queued",
                                            "ts": utc_ts(),
                                            "service_id": service_id,
                                            "process_id": process_id,
                                            "scope": {"username": scope_username, "session_id": scope_session_id},
                                            "goal_manager_service_id": goal_manager_service_id,
                                            "goal_manager_pending_count": len(queued_review),
                                        },
                                    )
                                    return
                                except Exception as exc:
                                    persist_goal_audit_failure(
                                        runtime_root=runtime_root,
                                        log_path=log_path,
                                        service_id=service_id,
                                        process_id=process_id,
                                        goal_audit_job_id="",
                                        username=scope_username,
                                        session_id=scope_session_id,
                                        error=repr(exc),
                                    )
                                    save_agent_audit_state(
                                        runtime_root,
                                        service_id=service_id,
                                        username=scope_username,
                                        session_id=scope_session_id,
                                        audit_state="panic",
                                    )
                                    previous_session = get_session_settings(
                                        runtime_root,
                                        username=scope_username,
                                        session_id=scope_session_id,
                                    )
                                    updated_session = update_session_goal_flags(
                                        runtime_root,
                                        username=scope_username,
                                        session_id=scope_session_id,
                                        goal_completed=False,
                                        goal_progress_state="in_progress",
                                    )
                                    emit_scoped_goal_status(updated_session, previous_session)
                                    maybe_spawn_failure_recovery(
                                        username=scope_username,
                                        session_id=scope_session_id,
                                        failure_event={
                                            "type": "service.goal_manager_compact_failed",
                                            "error": repr(exc),
                                            "service_id": service_id,
                                            "process_id": process_id,
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
                        if latest_goal_completed:
                            maybe_resume_parent_after_child_completion(
                                username=scope_username,
                                child_session_id=scope_session_id,
                                child_session_settings=latest_post_followup_settings,
                                completion_service_id=service_id,
                            )
                            latest_post_followup_settings = (
                                get_session_settings(
                                    runtime_root,
                                    username=scope_username,
                                    session_id=scope_session_id,
                                )
                                or latest_post_followup_settings
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
                                session_settings=latest_post_followup_settings,
                            )
                        may_auto_followup = bool(
                            latest_goal_text
                            and latest_goal_active
                            and not latest_goal_completed
                            and latest_goal_progress_state == "in_progress"
                            and latest_goal_audit_state == "all_clear"
                        )
                        (
                            followup_agent_id,
                            next_pending_inputs,
                            next_service_pending_inputs,
                            has_actionable_pending,
                        ) = _post_turn_followup_pending_state(
                            runtime_root,
                            username=scope_username,
                            session_id=scope_session_id,
                            service_id=service_id,
                            provider_session_slot=provider_session_slot,
                            explicit_agent_id=message_meta_get(message, "session_agent_id"),
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
                                "pending_input_count": len(next_pending_inputs),
                                "service_pending_input_count": len(next_service_pending_inputs),
                                "followup_agent_id": followup_agent_id,
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
                                    session_agent_id=followup_agent_id,
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
                        lifecycle_review = _maybe_enqueue_in_progress_goal_lifecycle_review(
                            runtime_root=runtime_root,
                            manifest=manifest,
                            process_id=process_id,
                            service_id=service_id,
                            provider_session_slot=provider_session_slot,
                            username=scope_username,
                            session_id=scope_session_id,
                            default_provider=default_provider,
                            send_tx=send_tx,
                            reason="non_goal_manager_turn_completed_without_owner",
                        )
                        if lifecycle_review is not None:
                            write_jsonl(
                                log_path,
                                {
                                    "type": "service.goal_manager_lifecycle_review",
                                    "ts": utc_ts(),
                                    "service_id": service_id,
                                    "process_id": process_id,
                                    "scope": {
                                        "username": scope_username,
                                        "session_id": scope_session_id,
                                    },
                                    **lifecycle_review,
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
        if max_turns >= 0 and reply_index >= max_turns and not done_sent.is_set():
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

            if max_turns >= 0 and current_reply_index >= max_turns:
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
