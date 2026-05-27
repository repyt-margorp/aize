from __future__ import annotations

import html
import json
import time
from pathlib import Path
from typing import Any

from kernel.registry import get_service_record, list_service_records
from runtime.event_log import make_history_event_entry
from runtime.message_builder import (
    make_aize_pending_input,
    make_dispatch_pending_message,
)
from runtime.dispatch_queue import dispatch_priority
from runtime.communication_goal import session_goal_completion_policy
from runtime.persistent_state_pkg import (
    append_history as append_user_history,
    append_goal_manager_pending_input,
    append_service_pending_input,
    claim_session_restart_resume,
    consume_session_due_auto_resume,
    get_history as get_user_history,
    get_session_settings,
    lease_session_service,
    load_agent_audit_state,
    load_codex_session,
    load_claude_session,
    load_gemini_session,
    load_goal_manager_pending_inputs,
    load_pending_inputs,
    read_json_file,
    load_service_pending_inputs,
    list_all_sessions_with_users,
    list_codex_sessions,
    list_claude_sessions,
    list_gemini_sessions,
    list_sessions_bound_to_service,
    normalize_auto_compact_threshold_left_percent,
    resolve_session_agent_id,
    session_goal_manager_state_path,
    session_service_state_path,
    session_dir,
    session_services_dir,
    session_timeline_path,
    update_session_context_status,
    write_json_file,
)
from runtime.providers import (
    run_claude_compaction,
    run_claude_context_check,
    run_codex_compaction,
    run_codex_context_check,
    run_gemini_compaction,
    run_gemini_context_check,
)
from runtime.restart_recovery import (
    GOAL_MANAGER_RUNNING_STALE_SECONDS,
    build_restart_resume_claim_run_id,
    has_actionable_pending_inputs,
    has_live_actionable_pending_inputs,
    history_has_dangling_goal_audit,
    history_has_terminal_goal_manager_cycle,
    history_has_unfinished_turn,
    latest_agent_turn_completed_at,
    latest_goal_manager_failure,
    latest_goal_manager_review,
    restart_resume_startup_budget as resolve_restart_resume_startup_budget,
    review_cursor_for_session,
    utc_ts_age_seconds,
)
from runtime.restart_panic_recovery import enqueue_restart_panic_recovery
from runtime.session_lifecycle import (
    purge_continuous_communication_restart_owner_lost_state,
)
from wire.protocol import encode_line, make_message, message_set_meta, utc_ts, write_jsonl

GOAL_AUDIT_HISTORY_LIMIT = 500


def _fallback_codex_session_id_for_conversation(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    preferred_service_id: str,
) -> str | None:
    service_dir = session_services_dir(runtime_root, username=username, session_id=session_id)
    if not service_dir.exists():
        return None

    service_ids: list[str] = []
    if preferred_service_id:
        service_ids.append(preferred_service_id)
    session_settings = get_session_settings(runtime_root, username=username, session_id=session_id) or {}
    for candidate in (
        str(session_settings.get("service_id") or "").strip(),
        str(session_settings.get("restart_resume_claim_service_id") or "").strip(),
    ):
        if candidate and candidate not in service_ids:
            service_ids.append(candidate)
    for path in sorted(service_dir.glob("service-*.json")):
        candidate = path.stem
        if candidate not in service_ids:
            service_ids.append(candidate)

    slot_priority = ("interactive_agent", "worker_agent")
    for candidate_service_id in service_ids:
        service_state = read_json_file(
            session_service_state_path(
                runtime_root,
                username=username,
                session_id=session_id,
                service_id=candidate_service_id,
            )
        )
        if not isinstance(service_state, dict):
            continue
        provider_sessions = service_state.get("provider_sessions")
        if not isinstance(provider_sessions, dict):
            continue
        for slot in slot_priority:
            slot_state = provider_sessions.get(slot)
            provider_session_id = (
                slot_state.get("codex_session_id")
                if isinstance(slot_state, dict)
                else None
            )
            if isinstance(provider_session_id, str) and provider_session_id.strip():
                return provider_session_id.strip()
        for slot_state in provider_sessions.values():
            provider_session_id = (
                slot_state.get("codex_session_id")
                if isinstance(slot_state, dict)
                else None
            )
            if isinstance(provider_session_id, str) and provider_session_id.strip():
                return provider_session_id.strip()
    return None


def context_status_from_history_entry(entry: dict[str, Any]) -> dict[str, str] | None:
    event_type = str(entry.get("event_type", ""))
    compaction = str(entry.get("context_compaction", ""))
    left_percent = entry.get("context_post_left_percent") or entry.get("context_left_percent")
    used_percent = entry.get("context_post_used_percent") or entry.get("context_used_percent")
    if (
        event_type in {"service.auto_compact_checked", "service.manual_compact_checked", "service.goal_manager_compact_checked"}
        and compaction == "triggered"
    ):
        pre_left_percent = entry.get("context_left_percent")
        post_left_percent = entry.get("context_post_left_percent")
        if post_left_percent is None or str(post_left_percent) == str(pre_left_percent):
            return {
                "label": "Context compacted",
                "meta": "Waiting for next context check to refresh the percentage",
                "compaction": compaction,
                "event_type": event_type,
            }
    if left_percent is None:
        return None
    return {
        "label": f"Context {left_percent}% left",
        "meta": "",
        "left_percent": str(left_percent),
        "used_percent": str(used_percent) if used_percent is not None else "",
        "compaction": compaction,
        "event_type": event_type,
    }


def persist_session_context_status(
    runtime_root: Path,
    *,
    username: str | None,
    session_id: str | None,
    event: dict[str, Any],
    service_id: str,
) -> None:
    if not (isinstance(username, str) and isinstance(session_id, str)):
        return
    entry = make_history_event_entry(event, service_id=service_id)
    status = context_status_from_history_entry(entry)
    if status is None and str(event.get("type")) in {
        "service.auto_compact_checked",
        "service.auto_compact_failed",
        "service.manual_compact_checked",
        "service.manual_compact_failed",
        "service.goal_manager_compact_checked",
        "service.goal_manager_compact_failed",
    }:
        status = {
            "label": entry.get("text", "Context unknown"),
            "meta": "",
            "event_type": str(event.get("type", "")),
            "compaction": str(event.get("compaction", "")),
        }
        left_percent = event.get("post_left_percent") or event.get("left_percent")
        used_percent = event.get("post_used_percent") or event.get("used_percent")
        if left_percent is not None:
            status["left_percent"] = str(left_percent)
        if used_percent is not None:
            status["used_percent"] = str(used_percent)
    update_session_context_status(
        runtime_root,
        username=username,
        session_id=session_id,
        context_status=status,
    )


def wait_for_service_record(runtime_root: Path, service_id: str, *, timeout_seconds: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            return get_service_record(runtime_root, service_id)
        except (KeyError, FileNotFoundError, json.JSONDecodeError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)


def maybe_resume_after_restart(
    *,
    runtime_root: Path,
    manifest: dict[str, Any],
    self_service: dict[str, Any],
    process_id: str,
    log_path: Path,
    service_id: str,
    router_conn: Any,
    service_kind: str = "codex",
) -> None:
    restart_resume = dict(self_service.get("config", {})).get("restart_resume")
    if not isinstance(restart_resume, dict):
        return
    restart_resume_startup_budget = resolve_restart_resume_startup_budget(self_service)
    restart_resume_startup_count = 0
    previous_status = str(restart_resume.get("previous_status") or "unknown")
    previous_process_id = str(restart_resume.get("previous_process_id") or "unknown")
    if service_kind == "claude":
        session_entries = list_claude_sessions(runtime_root, service_id=service_id)
    else:
        session_entries = list_codex_sessions(runtime_root, service_id=service_id)
    filtered_session_entries: list[dict[str, str | None]] = []
    for entry in session_entries:
        username = entry.get("username")
        session_id = entry.get("conversation_session_id") or entry.get("session_id")
        if not isinstance(username, str) or not isinstance(session_id, str):
            continue
        talk = get_session_settings(runtime_root, username=username, session_id=session_id) or {}
        preferred_provider = str(talk.get("preferred_provider") or service_kind).strip().lower() or service_kind
        bound_service_id = str(talk.get("service_id") or "").strip()
        if preferred_provider and preferred_provider != service_kind:
            continue
        if bound_service_id and bound_service_id != service_id:
            continue
        filtered_session_entries.append(entry)
    session_entries = filtered_session_entries
    session_entry_map: dict[tuple[str, str], dict[str, str | None]] = {}
    for entry in session_entries:
        username = entry.get("username")
        session_id = entry.get("conversation_session_id") or entry.get("session_id")
        if isinstance(username, str) and isinstance(session_id, str):
            session_entry_map[(username, session_id)] = entry
    fallback_entries: list[dict[str, str | None]] = []
    for bound in list_sessions_bound_to_service(runtime_root, service_id=service_id):
        username = bound.get("username")
        session_id = bound.get("session_id")
        if not isinstance(username, str) or not isinstance(session_id, str):
            continue
        if (username, session_id) in session_entry_map:
            continue
        fallback_entries.append(
            {
                "username": username,
                "conversation_session_id": session_id,
                "service_id": service_id,
                "recovery_mode": "reconstruct_without_session",
            }
        )
    candidate_entries = session_entries + fallback_entries
    active_scope_entries = set(session_entry_map)
    for talk in list_all_sessions_with_users(runtime_root):
        username = str(talk.get("username") or "").strip()
        session_id = str(talk.get("session_id") or "").strip()
        if not username or not session_id or (username, session_id) in active_scope_entries:
            continue
        preferred_provider = str(talk.get("preferred_provider") or service_kind).strip().lower() or service_kind
        bound_service_id = str(talk.get("service_id") or "").strip()
        if preferred_provider != service_kind:
            continue
        if bound_service_id and bound_service_id != service_id:
            continue
        goal_active = bool(talk.get("goal_active", False))
        goal_completed = bool(talk.get("goal_completed", False))
        goal_progress_state = str(
            talk.get("goal_progress_state", "complete" if goal_completed else "in_progress")
        ).strip().lower()
        if not goal_active or goal_completed or goal_progress_state != "in_progress":
            continue
        provider_session_id = None
        if service_kind == "claude":
            provider_session_id = load_claude_session(
                runtime_root,
                service_id=service_id,
                username=username,
                session_id=session_id,
            )
        elif service_kind == "gemini":
            provider_session_id = load_gemini_session(
                runtime_root,
                service_id=service_id,
                username=username,
                session_id=session_id,
            )
        else:
            provider_session_id = load_codex_session(
                runtime_root,
                service_id=service_id,
                username=username,
                session_id=session_id,
            )
        if provider_session_id:
            continue
        leased_service_id = lease_session_service(
            runtime_root,
            username=username,
            session_id=session_id,
            pool_service_ids=[service_id],
        )
        if leased_service_id != service_id:
            continue
        session_entry_map[(username, session_id)] = {
            "username": username,
            "conversation_session_id": session_id,
            "service_id": service_id,
            "recovery_mode": "goal_manager_review_only",
            "orphan_in_progress_session": "true",
        }
        candidate_entries.append(session_entry_map[(username, session_id)])
    if not candidate_entries:
        return

    def reconcile_stale_goal_manager_runtime(
        username: str,
        session_id: str,
        *,
        talk: dict[str, Any],
        should_standard_goal_route: bool,
    ) -> tuple[bool, str]:
        goal_manager_state_path = session_goal_manager_state_path(
            runtime_root,
            username=username,
            session_id=session_id,
        )
        goal_manager_state = read_json_file(goal_manager_state_path) or {}
        runtime_state = str(goal_manager_state.get("state") or "").strip().lower()
        if runtime_state != "running":
            return False, runtime_state
        queued_turn_completed = str(
            goal_manager_state.get("last_queued_turn_completed_at") or ""
        ).strip()
        latest_turn_completed = queued_turn_completed
        if not latest_turn_completed and not bool(talk.get("communication_agent_enabled", False)):
            latest_turn_completed = latest_agent_turn_completed_at(talk)
        review_cursor = review_cursor_for_session(
            runtime_root,
            username=username,
            session_id=session_id,
            talk=talk,
        )
        updated_at = str(goal_manager_state.get("updated_at") or "").strip()
        updated_age_seconds = utc_ts_age_seconds(updated_at)
        running_state_is_stale = updated_age_seconds is None or updated_age_seconds >= GOAL_MANAGER_RUNNING_STALE_SECONDS
        pending_work_items = goal_manager_state.get("pending_work_items")
        has_persisted_pending_work = isinstance(pending_work_items, list) and bool(pending_work_items)
        should_requeue_running_state = bool(
            should_standard_goal_route
            and running_state_is_stale
            and (
                has_persisted_pending_work
                or (
                    latest_turn_completed
                    and latest_turn_completed > review_cursor
                    and (not updated_at or updated_at < latest_turn_completed)
                )
            )
        )
        if should_requeue_running_state:
            if not has_persisted_pending_work:
                pending_work_items = [
                    {
                        "kind": "turn_completed",
                        "ts": latest_turn_completed,
                        "service_id": str(goal_manager_state.get("service_id") or ""),
                        "goal_id": str(
                            talk.get("active_goal_id")
                            or talk.get("goal_id")
                            or ""
                        ).strip(),
                    }
                ]
            for pending_work_item in pending_work_items:
                append_goal_manager_pending_input(
                    runtime_root,
                    username=username,
                    session_id=session_id,
                    entry=dict(pending_work_item),
                )
            goal_manager_state["state"] = "queued"
            goal_manager_state["pending_work_items"] = pending_work_items
            goal_manager_state["last_queued_turn_completed_at"] = str(latest_turn_completed or "").strip()
            goal_manager_state["stale_reason"] = (
                "persisted_pending_work_after_stale_running_state"
                if has_persisted_pending_work
                else "unreviewed_turn_completed_after_stale_running_state"
            )
            goal_manager_state["updated_at"] = utc_ts()
            write_json_file(goal_manager_state_path, goal_manager_state)
            write_jsonl(
                log_path,
                {
                    "type": "service.goal_audit_stale_reset",
                    "ts": utc_ts(),
                    "service_id": service_id,
                    "process_id": process_id,
                    "scope": {"username": username, "session_id": session_id},
                    "previous_state": runtime_state,
                    "latest_turn_completed_at": latest_turn_completed,
                    "last_reviewed_turn_completed_at": review_cursor,
                    "new_state": "queued",
                    "requeued_pending_count": len(pending_work_items),
                    "updated_at": updated_at,
                    "updated_age_seconds": updated_age_seconds,
                    "stale_after_seconds": GOAL_MANAGER_RUNNING_STALE_SECONDS,
                },
            )
            return True, "queued"
        return False, runtime_state

    for entry in candidate_entries:
        session_id = entry.get("session_id")
        scope_username = entry.get("username")
        scope_session_id = entry.get("conversation_session_id") or entry.get("session_id")
        if not isinstance(scope_username, str) or not isinstance(scope_session_id, str):
            continue
        session_slot = str(entry.get("slot") or "").strip().lower()
        recovery_mode = str(entry.get("recovery_mode") or ("resume" if session_id else "reconstruct_without_session"))
        pending_inputs = load_pending_inputs(runtime_root, username=scope_username, session_id=scope_session_id)
        goal_manager_pending_inputs = load_goal_manager_pending_inputs(
            runtime_root,
            username=scope_username,
            session_id=scope_session_id,
        )
        talk = get_session_settings(runtime_root, username=scope_username, session_id=scope_session_id) or {}
        due_auto_resume = consume_session_due_auto_resume(
            runtime_root,
            username=scope_username,
            session_id=scope_session_id,
        )
        if isinstance(due_auto_resume, dict):
            talk = due_auto_resume
        goal_text = str(talk.get("goal_text", "")).strip()
        goal_active = bool(talk.get("goal_active", False))
        goal_completed = bool(talk.get("goal_completed", False))
        goal_progress_state = str(
            talk.get("goal_progress_state", "complete" if goal_completed else "in_progress")
        ).strip().lower()
        # Audit state is agent-side
        service_id_for_entry = str(entry.get("service_id") or service_id)
        service_pending_inputs = load_service_pending_inputs(
            runtime_root,
            service_id=service_id_for_entry,
            agent_id=resolve_session_agent_id(
                runtime_root,
                username=scope_username,
                session_id=scope_session_id,
                service_id=service_id_for_entry,
                role=session_slot or None,
            ),
            username=scope_username,
            session_id=scope_session_id,
        )
        goal_audit_state = load_agent_audit_state(
            runtime_root,
            service_id=service_id_for_entry,
            username=scope_username,
            session_id=scope_session_id,
        )
        history = get_user_history(runtime_root, username=scope_username, session_id=scope_session_id)
        gm_failure_entry = latest_goal_manager_failure(history)
        unfinished_turn = history_has_unfinished_turn(history)
        has_actionable_pending = has_actionable_pending_inputs(pending_inputs) or has_actionable_pending_inputs(service_pending_inputs)
        has_live_actionable_pending = has_live_actionable_pending_inputs(pending_inputs) or has_live_actionable_pending_inputs(service_pending_inputs)
        is_continuous_communication_goal = bool(
            talk.get("communication_agent_enabled", False)
            and session_goal_completion_policy(talk) == "continuous"
        )
        if is_continuous_communication_goal:
            purge_continuous_communication_restart_owner_lost_state(
                runtime_root,
                username=scope_username,
                session_id=scope_session_id,
            )
            goal_manager_pending_inputs = load_goal_manager_pending_inputs(
                runtime_root,
                username=scope_username,
                session_id=scope_session_id,
            )
        should_standard_goal_route = bool(
            goal_text
            and goal_active
            and not goal_completed
            and goal_progress_state == "in_progress"
            and goal_audit_state in {"all_clear", "needs_compact"}
        )
        latest_review = latest_goal_manager_review(
            runtime_root,
            username=scope_username,
            session_id=scope_session_id,
        )
        # Restart dispatch enters through GoalManager. The latest review may
        # include worker continuation XML, but after a process restart the GM
        # should decide whether to close the goal, resume a worker, or spawn
        # follow-up child sessions.
        continue_feedback_enqueued = False
        stale_goal_manager_reset, _goal_manager_runtime_state = reconcile_stale_goal_manager_runtime(
            scope_username,
            scope_session_id,
            talk=talk,
            should_standard_goal_route=should_standard_goal_route,
        )
        latest_turn_completed_at = latest_agent_turn_completed_at(talk)
        last_reviewed_turn_completed_at = review_cursor_for_session(
            runtime_root,
            username=scope_username,
            session_id=scope_session_id,
            talk=talk,
        )
        has_unreviewed_turn_completed = bool(
            should_standard_goal_route
            and latest_turn_completed_at
            and latest_turn_completed_at > last_reviewed_turn_completed_at
        )
        dangling_goal_audit = should_standard_goal_route and history_has_dangling_goal_audit(history)
        goal_manager_review_reasons: list[str] = []
        if recovery_mode == "goal_manager_review_only":
            goal_manager_review_reasons.append("orphan_in_progress_goal")
        if dangling_goal_audit:
            goal_manager_review_reasons.append("dangling_goal_audit")
        if has_unreviewed_turn_completed:
            goal_manager_review_reasons.append("unreviewed_turn_completed")
        if stale_goal_manager_reset:
            goal_manager_review_reasons.append("stale_goal_manager_runtime")
        if goal_manager_pending_inputs:
            goal_manager_review_reasons.append("goal_manager_pending")
        if (
            should_standard_goal_route
            and not goal_manager_review_reasons
            and not is_continuous_communication_goal
        ):
            goal_manager_review_reasons.append("system_restart_active_in_progress")
        if (
            should_standard_goal_route
            and not unfinished_turn
            and not has_actionable_pending
            and not isinstance(due_auto_resume, dict)
            and recovery_mode != "reconstruct_without_session"
            and not goal_manager_review_reasons
            and not history_has_terminal_goal_manager_cycle(history)
            and not is_continuous_communication_goal
        ):
            goal_manager_review_reasons.append("orphan_in_progress_goal")
        restart_goal_manager_review_only = bool(
            should_standard_goal_route
            and not goal_completed
            and recovery_mode != "reconstruct_without_session"
            and goal_manager_review_reasons
        )
        should_resume_unfinished = (
            not goal_completed
            and (
                unfinished_turn
                or has_actionable_pending
                or restart_goal_manager_review_only
                or isinstance(due_auto_resume, dict)
                or continue_feedback_enqueued
            )
        )
        if (
            not should_resume_unfinished
            and goal_active
            and not goal_completed
            and goal_progress_state == "in_progress"
            and goal_audit_state == "panic"
            and not unfinished_turn
            and not has_actionable_pending
            and not isinstance(due_auto_resume, dict)
        ):
            panic_event = {
                "type": "agent_audit_panic_restart",
                "reason": "agent_audit_state_panic",
                "service_id": service_id_for_entry,
                "previous_status": previous_status,
                "previous_process_id": previous_process_id,
            }
            session_label = str(talk.get("label") or scope_session_id)
            preferred_provider = service_kind if service_kind in {"codex", "claude", "gemini"} else "codex"
            recovery_session_id = enqueue_restart_panic_recovery(
                runtime_root,
                manifest=manifest,
                process_id=process_id,
                log_path=log_path,
                router_conn=router_conn,
                username=scope_username,
                source_session_id=scope_session_id,
                source_label=session_label,
                panic_service_id=service_id_for_entry,
                panic_event=panic_event,
                preferred_provider=preferred_provider,
            )
            if recovery_session_id:
                continue
        if (
            not should_resume_unfinished
            and goal_active
            and not goal_completed
            and isinstance(gm_failure_entry, dict)
            and not unfinished_turn
            and not has_actionable_pending
            and not isinstance(due_auto_resume, dict)
        ):
            panic_event = dict(gm_failure_entry.get("event") or {})
            if not panic_event:
                panic_event = {
                    "type": str(gm_failure_entry.get("event_type") or "service.goal_manager_compact_failed"),
                    "error": str(gm_failure_entry.get("text") or "").strip(),
                }
            session_label = str(talk.get("label") or scope_session_id)
            preferred_provider = service_kind if service_kind in {"codex", "claude", "gemini"} else "codex"
            recovery_session_id = enqueue_restart_panic_recovery(
                runtime_root,
                manifest=manifest,
                process_id=process_id,
                log_path=log_path,
                router_conn=router_conn,
                username=scope_username,
                source_session_id=scope_session_id,
                source_label=session_label,
                panic_service_id=service_id_for_entry,
                panic_event=panic_event,
                preferred_provider=preferred_provider,
            )
            if recovery_session_id:
                continue
        if not should_resume_unfinished:
            write_jsonl(
                log_path,
                {
                    "type": "service.restart_resume_skipped",
                    "ts": utc_ts(),
                    "service_id": service_id,
                    "process_id": process_id,
                    "scope": {"username": scope_username, "session_id": scope_session_id},
                    "session_id": session_id,
                    "recovery_mode": recovery_mode,
                    "reason": (
                        "goal_already_completed"
                        if goal_completed and (unfinished_turn or has_actionable_pending)
                        else "idle_without_unfinished_turn_or_actionable_pending"
                    ),
                    "unfinished_turn": unfinished_turn,
                    "has_actionable_pending": has_actionable_pending,
                    "dangling_goal_audit": dangling_goal_audit,
                    "has_unreviewed_turn_completed": has_unreviewed_turn_completed,
                    "latest_turn_completed_at": latest_turn_completed_at,
                    "last_reviewed_turn_completed_at": last_reviewed_turn_completed_at,
                    "stale_goal_manager_reset": stale_goal_manager_reset,
                    "due_auto_resume": bool(isinstance(due_auto_resume, dict)),
                    "should_standard_goal_route": should_standard_goal_route,
                },
            )
            continue
        is_startup_recovery_only = bool(
            not has_live_actionable_pending
            and not isinstance(due_auto_resume, dict)
            and not should_standard_goal_route
            and recovery_mode != "reconstruct_without_session"
        )
        if is_startup_recovery_only and restart_resume_startup_count >= restart_resume_startup_budget:
            write_jsonl(
                log_path,
                {
                    "type": "service.restart_resume_skipped",
                    "ts": utc_ts(),
                    "service_id": service_id,
                    "process_id": process_id,
                    "scope": {"username": scope_username, "session_id": scope_session_id},
                    "session_id": session_id,
                    "recovery_mode": recovery_mode,
                    "reason": "restart_resume_startup_budget_exhausted",
                    "restart_resume_startup_budget": restart_resume_startup_budget,
                    "unfinished_turn": unfinished_turn,
                    "has_actionable_pending": has_actionable_pending,
                    "has_live_actionable_pending": has_live_actionable_pending,
                    "dangling_goal_audit": dangling_goal_audit,
                    "has_unreviewed_turn_completed": has_unreviewed_turn_completed,
                    "due_auto_resume": bool(isinstance(due_auto_resume, dict)),
                    "should_standard_goal_route": should_standard_goal_route,
                },
            )
            continue
        restart_generation_id = str(manifest.get("run_id") or manifest.get("node_id") or "system-restart").strip()
        restart_claim_slot = "goal_manager" if restart_goal_manager_review_only else (session_slot or "agent")
        run_id = build_restart_resume_claim_run_id(
            restart_generation_id=restart_generation_id,
            scope_session_id=scope_session_id,
            restart_claim_slot=restart_claim_slot,
            service_id=service_id,
        )
        if not claim_session_restart_resume(
            runtime_root,
            username=scope_username,
            session_id=scope_session_id,
            run_id=run_id,
            service_id=service_id,
        ):
            write_jsonl(
                log_path,
                {
                    "type": "service.restart_resume_skipped",
                    "ts": utc_ts(),
                    "service_id": service_id,
                    "process_id": process_id,
                    "scope": {"username": scope_username, "session_id": scope_session_id},
                    "session_id": session_id,
                    "recovery_mode": recovery_mode,
                    "reason": "restart_resume_already_claimed_for_run",
                    "unfinished_turn": unfinished_turn,
                    "has_actionable_pending": has_actionable_pending,
                    "dangling_goal_audit": dangling_goal_audit,
                    "has_unreviewed_turn_completed": has_unreviewed_turn_completed,
                    "latest_turn_completed_at": latest_turn_completed_at,
                    "last_reviewed_turn_completed_at": last_reviewed_turn_completed_at,
                    "stale_goal_manager_reset": stale_goal_manager_reset,
                    "due_auto_resume": bool(isinstance(due_auto_resume, dict)),
                    "should_standard_goal_route": should_standard_goal_route,
                },
            )
            continue
        if (
            not restart_goal_manager_review_only
            and (
                unfinished_turn
                or has_actionable_pending
                or isinstance(due_auto_resume, dict)
                or recovery_mode == "reconstruct_without_session"
            )
        ):
            scope_session_dir = session_dir(
                runtime_root,
                username=scope_username,
                session_id=scope_session_id,
            )
            scope_timeline_path = session_timeline_path(
                runtime_root,
                username=scope_username,
                session_id=scope_session_id,
            )
            append_service_pending_input(
                runtime_root,
                service_id=service_id,
                agent_id=resolve_session_agent_id(
                    runtime_root,
                    username=scope_username,
                    session_id=scope_session_id,
                    service_id=service_id,
                    role=session_slot or None,
                ),
                username=scope_username,
                session_id=scope_session_id,
                entry=make_aize_pending_input(
                    kind="scheduled_resume" if isinstance(due_auto_resume, dict) else "restart_resume",
                    role="system",
                    text="\n".join(
                        [
                            "<aize_restart_resume>",
                            f"  <reason>{'scheduled_resume' if isinstance(due_auto_resume, dict) else 'system_restart'}</reason>",
                            f"  <recovery_mode>{html.escape(recovery_mode)}</recovery_mode>",
                            f"  <service_id>{html.escape(service_id)}</service_id>",
                            f"  <session_id>{html.escape(str(session_id or ''))}</session_id>",
                            f"  <previous_status>{html.escape(previous_status)}</previous_status>",
                            f"  <previous_process_id>{html.escape(previous_process_id)}</previous_process_id>",
                            "  <current_status>running</current_status>",
                            f"  <current_process_id>{html.escape(process_id)}</current_process_id>",
                            f"  <session_dir>{html.escape(str(scope_session_dir))}</session_dir>",
                            f"  <timeline_path>{html.escape(str(scope_timeline_path))}</timeline_path>",
                            (
                                "  <instruction>Resume the interrupted work from the current provider session context. Do not spend this turn on a status-only acknowledgment, a plan-only reply, or a statement that you will continue. Continue editing, testing, or executing the next concrete step immediately, and only report progress after advancing the work.</instruction>"
                                if session_id
                                else "  <instruction>The previous provider session was not recoverable. Reconstruct the unfinished work from the recent talk history, resume the concrete task immediately, and do not spend this turn on a status-only acknowledgment or a plan-only reply.</instruction>"
                            ),
                            "  <history_instruction>Read the session files directly for detailed prior events and pending work instead of relying on inline excerpts.</history_instruction>",
                            "</aize_restart_resume>",
                        ]
                    ),
                ),
            )
        if restart_goal_manager_review_only:
            goal_manager_work_item = {
                "kind": "restart_goal_review",
                "ts": utc_ts(),
                "service_id": service_id,
                "goal_id": str(talk.get("active_goal_id") or talk.get("goal_id") or "").strip(),
                "reason": "system_restart_in_progress_goal",
            }
            append_service_pending_input(
                runtime_root,
                service_id=service_id,
                agent_id=resolve_session_agent_id(
                    runtime_root,
                    username=scope_username,
                    session_id=scope_session_id,
                    service_id=service_id,
                    role="goal_manager",
                ),
                username=scope_username,
                session_id=scope_session_id,
                entry=make_aize_pending_input(
                    kind="goal_manager_review",
                    role="system",
                    text=json.dumps(goal_manager_work_item, ensure_ascii=False),
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
                    "service_id": service_id,
                    "pending_work_items": [goal_manager_work_item],
                    "updated_at": utc_ts(),
                }
            )
            write_json_file(goal_manager_state_path, goal_manager_state)
        dispatch_message = make_dispatch_pending_message(
            manifest=manifest,
            from_service_id="service-svcmgr-001",
            to_service_id=service_id,
            process_id=process_id,
            run_id=run_id,
            username=scope_username,
            session_id=scope_session_id,
            auth_context=None,
            reason="goal_manager_review" if restart_goal_manager_review_only else "restart_resume",
            session_agent_id=resolve_session_agent_id(
                runtime_root,
                username=scope_username,
                session_id=scope_session_id,
                service_id=service_id,
                role="goal_manager" if restart_goal_manager_review_only else (session_slot or None),
            ),
            agent_profile=(
                {"session_slot": "goal_manager"}
                if restart_goal_manager_review_only
                else ({"session_slot": session_slot} if session_slot else None)
            ),
            dispatch_priority=dispatch_priority(
                "goal_manager_review" if restart_goal_manager_review_only else "restart_resume"
            ),
        )
        router_conn.write(encode_line(dispatch_message))
        if is_startup_recovery_only:
            restart_resume_startup_count += 1
        restart_resume_event = {
            "type": "service.restart_resume_enqueued",
            "ts": utc_ts(),
            "service_id": service_id,
            "process_id": process_id,
            "scope": {"username": scope_username, "session_id": scope_session_id},
            "session_id": session_id,
            "recovery_mode": recovery_mode,
            "unfinished_turn": unfinished_turn,
            "has_actionable_pending": has_actionable_pending,
            "dangling_goal_audit": dangling_goal_audit,
            "has_unreviewed_turn_completed": has_unreviewed_turn_completed,
            "latest_turn_completed_at": latest_turn_completed_at,
            "last_reviewed_turn_completed_at": last_reviewed_turn_completed_at,
            "stale_goal_manager_reset": stale_goal_manager_reset,
            "goal_manager_review_only": restart_goal_manager_review_only,
            "goal_manager_review_reasons": goal_manager_review_reasons,
            "due_auto_resume": bool(isinstance(due_auto_resume, dict)),
            "goal_standard_route": should_standard_goal_route,
            "startup_recovery_only": is_startup_recovery_only,
            "restart_resume_startup_budget": restart_resume_startup_budget,
        }
        write_jsonl(log_path, restart_resume_event)
        append_user_history(
            runtime_root,
            username=scope_username,
            session_id=scope_session_id,
            entry={
                "direction": "session_input",
                "kind": (
                    "goal_manager_review"
                    if restart_goal_manager_review_only
                    else ("scheduled_resume" if isinstance(due_auto_resume, dict) else "restart_resume")
                ),
                "ts": utc_ts(),
                "service_id": service_id,
                "to": service_id,
                "text": (
                    "システム再起動後に未完了Goalの状態を確認するため、GoalManagerレビューをキューに入れました。"
                    if restart_goal_manager_review_only
                    else (
                        "自動再開時刻に到達したため、最新 Goal を再開する指示を自分のFIFOへ送りました。"
                        if isinstance(due_auto_resume, dict)
                        else f"システムが再起動しました。前の作業を続けるため、自分のFIFOに継続指示を送りました（process {previous_process_id} → {process_id}）。"
                    )
                ),
            },
            limit=GOAL_AUDIT_HISTORY_LIMIT,
        )


def emit_codex_compaction_event(
    *,
    runtime_root: Path,
    manifest: dict[str, Any],
    service_id: str,
    process_id: str,
    log_path: Path,
    tx_handle: Any,
    sender_service_id: str,
    run_id: str,
    scope_username: str | None,
    scope_session_id: str | None,
    event: dict[str, Any],
) -> None:
    write_jsonl(
        log_path,
        {
            "type": event["type"],
            "ts": utc_ts(),
            "service_id": service_id,
            "process_id": process_id,
            "run_id": run_id,
            "event": event,
        },
    )
    persist_session_context_status(runtime_root, username=scope_username, session_id=scope_session_id, event=event, service_id=service_id)

    if isinstance(scope_username, str) and isinstance(scope_session_id, str):
        event_entry = make_history_event_entry(event, service_id=service_id)
        event_message = make_message(
            from_node_id=manifest["node_id"],
            from_service_id=service_id,
            to_node_id=manifest["node_id"],
            to_service_id=sender_service_id,
            message_type="event",
            payload={"entry": event_entry},
            run_id=run_id,
        )
        message_set_meta(event_message, "process_id", process_id)
        message_set_meta(event_message, "conversation", {"username": scope_username, "session_id": scope_session_id})
        tx_handle.write(encode_line(event_message))
        tx_handle.flush()


def resolve_session_auto_compact_threshold(runtime_root: Path, *, username: str, session_id: str) -> int:
    talk = get_session_settings(runtime_root, username=username, session_id=session_id)
    if isinstance(talk, dict):
        return normalize_auto_compact_threshold_left_percent(
            talk.get("auto_compact_threshold_left_percent")
        )
    return normalize_auto_compact_threshold_left_percent(None)


def manual_compact_codex_session(
    *,
    repo_root: Path,
    runtime_root: Path,
    service_id: str,
    username: str,
    session_id: str,
) -> tuple[int, dict[str, Any], dict[str, Any] | None]:
    conversation_session_id = session_id
    session_id = load_codex_session(
        runtime_root,
        service_id=service_id,
        username=username,
        session_id=conversation_session_id,
    )
    if not session_id:
        session_id = _fallback_codex_session_id_for_conversation(
            runtime_root,
            username=username,
            session_id=conversation_session_id,
            preferred_service_id=service_id,
        )
    if not session_id:
        return (
            409,
            {"error": "codex_session_not_found", "service_id": service_id, "session_id": session_id},
            None,
        )
    event, returncode = run_codex_compaction(
        repo_root=repo_root,
        session_id=str(session_id),
        threshold_left_percent=101,
        mode="manual",
    )
    return (
        200 if returncode == 0 else 500,
        {"ok": returncode == 0, "service_id": service_id, "session_id": session_id, "event": event},
        make_history_event_entry(event, service_id=service_id),
    )


def goal_manager_compact_codex_session(
    *,
    repo_root: Path,
    runtime_root: Path,
    service_id: str,
    username: str,
    session_id: str,
) -> tuple[dict[str, Any], int]:
    conversation_session_id = session_id
    session_id = load_codex_session(
        runtime_root,
        service_id=service_id,
        username=username,
        session_id=conversation_session_id,
    )
    if not session_id:
        session_id = _fallback_codex_session_id_for_conversation(
            runtime_root,
            username=username,
            session_id=conversation_session_id,
            preferred_service_id=service_id,
        )
    if not session_id:
        return (
            {
                "type": "service.goal_manager_compact_failed",
                "session_id": "",
                "error": "codex_session_not_found",
                "session_id": session_id,
            },
            1,
        )
    return run_codex_compaction(
        repo_root=repo_root,
        session_id=str(session_id),
        threshold_left_percent=101,
        mode="goal_manager",
    )


def maybe_auto_compact_codex_session(
    *,
    runtime_root: Path,
    manifest: dict[str, Any],
    service_id: str,
    process_id: str,
    log_path: Path,
    tx_handle: Any,
    sender_service_id: str,
    run_id: str,
    scope_username: str | None,
    scope_session_id: str | None,
    session_id: str | None,
    threshold_left_percent: int,
) -> None:
    if not session_id:
        return
    repo_root = Path(__file__).resolve().parents[2]
    event, _returncode = run_codex_context_check(
        repo_root=repo_root,
        session_id=str(session_id),
        threshold_left_percent=threshold_left_percent,
    )
    emit_codex_compaction_event(
        runtime_root=runtime_root,
        manifest=manifest,
        service_id=service_id,
        process_id=process_id,
        log_path=log_path,
        tx_handle=tx_handle,
        sender_service_id=sender_service_id,
        run_id=run_id,
        scope_username=scope_username,
        scope_session_id=scope_session_id,
        event=event,
    )


def manual_compact_claude_session(
    *,
    repo_root: Path,
    runtime_root: Path,
    service_id: str,
    username: str,
    session_id: str,
) -> tuple[int, dict[str, Any], dict[str, Any] | None]:
    session_id = load_claude_session(
        runtime_root,
        service_id=service_id,
        username=username,
        session_id=session_id,
    )
    if not session_id:
        return (
            409,
            {"error": "claude_session_not_found", "service_id": service_id, "session_id": session_id},
            None,
        )
    event, returncode = run_claude_compaction(
        repo_root=repo_root,
        session_id=str(session_id),
        threshold_left_percent=101,
        mode="manual",
    )
    return (
        200 if returncode == 0 else 500,
        {"ok": returncode == 0, "service_id": service_id, "session_id": session_id, "event": event},
        make_history_event_entry(event, service_id=service_id),
    )


def manual_compact_gemini_session(
    *,
    repo_root: Path,
    runtime_root: Path,
    service_id: str,
    username: str,
    session_id: str,
) -> tuple[int, dict[str, Any], dict[str, Any] | None]:
    provider_session_id = load_gemini_session(
        runtime_root,
        service_id=service_id,
        username=username,
        session_id=session_id,
    )
    if not provider_session_id:
        return (
            409,
            {"error": "gemini_session_not_found", "service_id": service_id, "session_id": session_id},
            None,
        )
    event, _returncode = run_gemini_compaction(
        repo_root=repo_root,
        session_id=str(provider_session_id),
        threshold_left_percent=101,
        mode="manual",
    )
    return (
        200,
        {"ok": True, "service_id": service_id, "session_id": str(provider_session_id), "event": event},
        make_history_event_entry(event, service_id=service_id),
    )


def goal_manager_compact_gemini_session(
    *,
    repo_root: Path,
    runtime_root: Path,
    service_id: str,
    username: str,
    session_id: str,
) -> tuple[dict[str, Any], int]:
    provider_session_id = load_gemini_session(
        runtime_root,
        service_id=service_id,
        username=username,
        session_id=session_id,
    )
    if not provider_session_id:
        return (
            {
                "type": "service.goal_manager_compact_failed",
                "session_id": "",
                "error": "gemini_session_not_found",
            },
            1,
        )
    return run_gemini_compaction(
        repo_root=repo_root,
        session_id=str(provider_session_id),
        threshold_left_percent=101,
        mode="goal_manager",
    )


def maybe_auto_compact_gemini_session(
    *,
    runtime_root: Path,
    manifest: dict[str, Any],
    service_id: str,
    process_id: str,
    log_path: Path,
    tx_handle: Any,
    sender_service_id: str,
    run_id: str,
    scope_username: str | None,
    scope_session_id: str | None,
    session_id: str | None,
    threshold_left_percent: int,
) -> None:
    if not session_id:
        return
    repo_root = Path(__file__).resolve().parents[2]
    event, _returncode = run_gemini_context_check(
        repo_root=repo_root,
        session_id=str(session_id),
        threshold_left_percent=threshold_left_percent,
    )
    emit_codex_compaction_event(
        runtime_root=runtime_root,
        manifest=manifest,
        service_id=service_id,
        process_id=process_id,
        log_path=log_path,
        tx_handle=tx_handle,
        sender_service_id=sender_service_id,
        run_id=run_id,
        scope_username=scope_username,
        scope_session_id=scope_session_id,
        event=event,
    )


def manual_compact_clears_audit_state(status: int, response: dict[str, Any]) -> bool:
    if status != 200 or not bool(response.get("ok")):
        return False
    event = response.get("event")
    if not isinstance(event, dict):
        return False
    event_type = str(event.get("type") or "").strip().lower()
    if event_type != "service.manual_compact_checked":
        return False
    compaction = str(event.get("compaction") or "").strip().lower()
    return compaction in {"triggered", "skipped"}


def goal_manager_compact_claude_session(
    *,
    repo_root: Path,
    runtime_root: Path,
    service_id: str,
    username: str,
    session_id: str,
) -> tuple[dict[str, Any], int]:
    session_id = load_claude_session(
        runtime_root,
        service_id=service_id,
        username=username,
        session_id=session_id,
    )
    if not session_id:
        return (
            {
                "type": "service.goal_manager_compact_failed",
                "session_id": session_id,
                "error": "claude_session_not_found",
            },
            1,
        )
    return run_claude_compaction(
        repo_root=repo_root,
        session_id=str(session_id),
        threshold_left_percent=101,
        mode="goal_manager",
    )


def maybe_auto_compact_claude_session(
    *,
    runtime_root: Path,
    manifest: dict[str, Any],
    service_id: str,
    process_id: str,
    log_path: Path,
    tx_handle: Any,
    sender_service_id: str,
    run_id: str,
    scope_username: str | None,
    scope_session_id: str | None,
    session_id: str | None,
    threshold_left_percent: int,
) -> None:
    if not session_id:
        return
    repo_root = Path(__file__).resolve().parents[2]
    event, _returncode = run_claude_context_check(
        repo_root=repo_root,
        session_id=str(session_id),
        threshold_left_percent=threshold_left_percent,
    )
    emit_codex_compaction_event(
        runtime_root=runtime_root,
        manifest=manifest,
        service_id=service_id,
        process_id=process_id,
        log_path=log_path,
        tx_handle=tx_handle,
        sender_service_id=sender_service_id,
        run_id=run_id,
        scope_username=scope_username,
        scope_session_id=scope_session_id,
        event=event,
    )
