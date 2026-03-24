from __future__ import annotations

import html
import json
import time
from pathlib import Path
from typing import Any

from kernel.registry import get_service_record
from runtime.event_log import make_history_event_entry
from runtime.message_builder import (
    make_aize_pending_input,
    make_dispatch_pending_message,
)
from runtime.panic_recovery import (
    ensure_panic_recovery_session,
    panic_recovery_bootstrap_xml,
)
from runtime.persistent_state import (
    append_history as append_user_history,
    append_pending_input,
    append_service_pending_input,
    consume_session_due_auto_resume,
    get_history as get_user_history,
    get_session_settings,
    load_agent_audit_state,
    load_codex_session,
    load_claude_session,
    load_pending_inputs,
    read_json_file,
    load_service_pending_inputs,
    list_codex_sessions,
    list_claude_sessions,
    list_sessions_bound_to_service,
    normalize_auto_compact_threshold_left_percent,
    resolve_session_agent_id,
    session_goal_manager_reviews_path,
    session_goal_manager_state_path,
    session_dir,
    session_timeline_path,
    update_session_context_status,
    write_json_file,
)
from runtime.providers import (
    run_claude_compaction,
    run_claude_context_check,
    run_codex_compaction,
    run_codex_context_check,
)
from wire.protocol import encode_line, make_message, message_set_meta, utc_ts, write_jsonl

GOAL_AUDIT_HISTORY_LIMIT = 500


def _latest_goal_manager_review(runtime_root: Path, *, username: str, session_id: str) -> dict[str, Any] | None:
    path = session_goal_manager_reviews_path(runtime_root, username=username, session_id=session_id)
    if not path.exists():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for raw_line in reversed(lines):
        line = raw_line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            return record
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
    if not candidate_entries:
        return

    def has_unfinished_turn(username: str, session_id: str) -> bool:
        history = get_user_history(runtime_root, username=username, session_id=session_id)
        last_user_out_ts = ""
        last_turn_completed_ts = ""
        last_turn_activity_ts = ""
        for entry in history:
            ts = str(entry.get("ts") or "")
            direction = str(entry.get("direction") or "")
            event_type = str(entry.get("event_type") or "")
            if direction == "out":
                last_user_out_ts = ts
            if event_type in {
                "agent.turn_started",
                "thread.started",
                "turn.started",
                "item.started",
            }:
                if ts >= last_turn_activity_ts:
                    last_turn_activity_ts = ts
            if direction == "in":
                if ts >= last_turn_activity_ts:
                    last_turn_activity_ts = ts
            if event_type == "turn.completed":
                last_turn_completed_ts = ts
        if last_turn_activity_ts and last_turn_activity_ts > last_turn_completed_ts:
            return True
        if not last_user_out_ts:
            return False
        if not last_turn_completed_ts:
            return True
        return last_user_out_ts > last_turn_completed_ts

    def has_actionable_pending_inputs(pending_inputs: list[dict[str, Any]]) -> bool:
        return any(str(item.get("kind", "")) not in {"turn_completed", "scheduled_resume"} for item in pending_inputs)

    def has_dangling_goal_audit(username: str, session_id: str) -> bool:
        history = get_user_history(runtime_root, username=username, session_id=session_id)
        last_started_ts = ""
        last_terminal_ts = ""
        for record in history:
            event_type = str(record.get("event_type") or "")
            ts = str(record.get("ts") or "")
            if event_type == "service.goal_audit_started":
                last_started_ts = ts
            elif event_type in {"service.goal_audit_completed", "service.goal_audit_failed"}:
                last_terminal_ts = ts
        return bool(last_started_ts) and last_started_ts > last_terminal_ts

    def latest_goal_manager_failure(history: list[dict[str, Any]]) -> dict[str, Any] | None:
        for record in reversed(history):
            event_type = str(record.get("event_type") or "").strip()
            if event_type in {"service.goal_audit_failed", "service.goal_manager_compact_failed"}:
                return record
            if event_type in {"service.goal_audit_completed", "turn.completed"}:
                return None
        return None

    def latest_agent_turn_completed_at(talk: dict[str, Any]) -> str:
        welcomed_agents = talk.get("welcomed_agents")
        if not isinstance(welcomed_agents, list):
            return ""
        latest = ""
        for item in welcomed_agents:
            if not isinstance(item, dict):
                continue
            completed_at = str(item.get("last_turn_completed_at") or "").strip()
            if completed_at and completed_at > latest:
                latest = completed_at
        return latest

    def review_cursor_for_session(username: str, session_id: str, talk: dict[str, Any]) -> str:
        cursor = str(talk.get("goal_manager_last_reviewed_turn_completed_at") or "").strip()
        goal_manager_state = read_json_file(
            session_goal_manager_state_path(runtime_root, username=username, session_id=session_id)
        ) or {}
        state_cursor = str(goal_manager_state.get("last_reviewed_turn_completed_at") or "").strip()
        if state_cursor > cursor:
            cursor = state_cursor
        return cursor

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
        latest_turn_completed = latest_agent_turn_completed_at(talk)
        review_cursor = review_cursor_for_session(username, session_id, talk)
        updated_at = str(goal_manager_state.get("updated_at") or "").strip()
        if (
            should_standard_goal_route
            and latest_turn_completed
            and latest_turn_completed > review_cursor
            and (not updated_at or updated_at < latest_turn_completed)
        ):
            goal_manager_state["state"] = "idle"
            goal_manager_state["stale_reason"] = "unreviewed_turn_completed_after_stale_running_state"
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
                },
            )
            return True, "idle"
        return False, runtime_state

    for entry in candidate_entries:
        session_id = entry.get("session_id")
        scope_username = entry.get("username")
        scope_session_id = entry.get("conversation_session_id") or entry.get("session_id")
        if not isinstance(scope_username, str) or not isinstance(scope_session_id, str):
            continue
        recovery_mode = str(entry.get("recovery_mode") or ("resume" if session_id else "reconstruct_without_session"))
        pending_inputs = load_pending_inputs(runtime_root, username=scope_username, session_id=scope_session_id)
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
        unfinished_turn = has_unfinished_turn(scope_username, scope_session_id)
        has_actionable_pending = has_actionable_pending_inputs(pending_inputs) or has_actionable_pending_inputs(service_pending_inputs)
        should_standard_goal_route = bool(
            goal_text
            and goal_active
            and not goal_completed
            and goal_progress_state == "in_progress"
            and goal_audit_state in {"all_clear", "needs_compact"}
        )
        latest_review = _latest_goal_manager_review(
            runtime_root,
            username=scope_username,
            session_id=scope_session_id,
        )
        if (
            should_standard_goal_route
            and not has_actionable_pending
            and not unfinished_turn
            and isinstance(latest_review, dict)
            and str(latest_review.get("progress_state") or "").strip().lower() == "in_progress"
            and str(latest_review.get("audit_state") or "").strip().lower() == "all_clear"
        ):
            continue_xml = str(latest_review.get("continue_xml") or "").strip()
            if continue_xml:
                append_pending_input(
                    runtime_root,
                    username=scope_username,
                    session_id=scope_session_id,
                    entry=make_aize_pending_input(
                        kind="goal_feedback",
                        role="system",
                        text=continue_xml,
                    ),
                )
                pending_inputs = load_pending_inputs(
                    runtime_root,
                    username=scope_username,
                    session_id=scope_session_id,
                )
                has_actionable_pending = has_actionable_pending_inputs(pending_inputs) or has_actionable_pending_inputs(service_pending_inputs)
        stale_goal_manager_reset, _goal_manager_runtime_state = reconcile_stale_goal_manager_runtime(
            scope_username,
            scope_session_id,
            talk=talk,
            should_standard_goal_route=should_standard_goal_route,
        )
        latest_turn_completed_at = latest_agent_turn_completed_at(talk)
        last_reviewed_turn_completed_at = review_cursor_for_session(scope_username, scope_session_id, talk)
        has_unreviewed_turn_completed = bool(
            should_standard_goal_route
            and latest_turn_completed_at
            and latest_turn_completed_at > last_reviewed_turn_completed_at
        )
        dangling_goal_audit = should_standard_goal_route and has_dangling_goal_audit(scope_username, scope_session_id)
        should_resume_unfinished = (
            not goal_completed
            and (
                unfinished_turn
                or has_actionable_pending
                or dangling_goal_audit
                or has_unreviewed_turn_completed
                or isinstance(due_auto_resume, dict)
            )
        )
        # If the provider session itself is gone after restart, an active in-progress talk still
        # needs a reconstructive restart turn even when no pending queue entry survived.
        if (
            not should_resume_unfinished
            and recovery_mode == "reconstruct_without_session"
            and goal_active
            and not goal_completed
            and goal_progress_state == "in_progress"
        ):
            should_resume_unfinished = True
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
                    "type": str(gm_failure_entry.get("event_type") or "service.goal_audit_failed"),
                    "error": str(gm_failure_entry.get("text") or "").strip(),
                }
            session_label = str(talk.get("label") or scope_session_id)
            preferred_provider = "claude" if service_kind == "claude" else "codex"
            recovery_session = ensure_panic_recovery_session(
                runtime_root,
                username=scope_username,
                source_session_id=scope_session_id,
                source_label=session_label,
                panic_service_id=service_id,
                event=panic_event,
                preferred_provider=preferred_provider,
            )
            if isinstance(recovery_session, dict):
                recovery_session_id = str(recovery_session.get("session_id") or "").strip()
                if recovery_session_id:
                    append_pending_input(
                        runtime_root,
                        username=scope_username,
                        session_id=recovery_session_id,
                        entry=make_aize_pending_input(
                            kind="panic_recovery",
                            role="system",
                            text=panic_recovery_bootstrap_xml(
                                source_session_id=scope_session_id,
                                source_label=session_label,
                                panic_service_id=service_id,
                                event=panic_event,
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
                            "event_type": "service.panic_recovery_session_created",
                            "text": f"Panic recovery session created: {recovery_session_id}",
                            "event": {
                                "type": "service.panic_recovery_session_created",
                                "source_session_id": scope_session_id,
                                "recovery_session_id": recovery_session_id,
                                "panic_service_id": service_id,
                                "panic_event": panic_event,
                            },
                        },
                        limit=GOAL_AUDIT_HISTORY_LIMIT,
                    )
                    dispatch_message = make_dispatch_pending_message(
                        manifest=manifest,
                        from_service_id="service-http-001",
                        to_service_id=service_id,
                        process_id=process_id,
                        run_id=f"restart-recovery-{int(time.time())}",
                        username=scope_username,
                        session_id=recovery_session_id,
                        auth_context=None,
                        reason="restart_recovery",
                        session_agent_id=resolve_session_agent_id(
                            runtime_root,
                            username=scope_username,
                            session_id=recovery_session_id,
                            service_id=service_id,
                        ),
                    )
                    router_conn.write(encode_line(dispatch_message))
                    write_jsonl(
                        log_path,
                        {
                            "type": "service.restart_recovery_enqueued",
                            "ts": utc_ts(),
                            "service_id": service_id,
                            "process_id": process_id,
                            "scope": {"username": scope_username, "session_id": scope_session_id},
                            "recovery_session_id": recovery_session_id,
                            "panic_event_type": str(panic_event.get("type") or ""),
                        },
                    )
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
        run_id = f"system-restart-{int(time.time())}"
        if (
            unfinished_turn
            or has_actionable_pending
            or dangling_goal_audit
            or isinstance(due_auto_resume, dict)
            or recovery_mode == "reconstruct_without_session"
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
        dispatch_message = make_dispatch_pending_message(
            manifest=manifest,
            from_service_id="service-http-001",
            to_service_id=service_id,
            process_id=process_id,
            run_id=run_id,
            username=scope_username,
            session_id=scope_session_id,
            auth_context=None,
            reason="restart_resume",
            session_agent_id=resolve_session_agent_id(
                runtime_root,
                username=scope_username,
                session_id=scope_session_id,
                service_id=service_id,
            ),
        )
        router_conn.write(encode_line(dispatch_message))
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
            "due_auto_resume": bool(isinstance(due_auto_resume, dict)),
            "goal_standard_route": should_standard_goal_route,
        }
        write_jsonl(log_path, restart_resume_event)
        # Emit a virtual agent turn so the UI shows a restart agent box, separate from the
        # continuation turn that will follow when the agent processes the queued restart_resume input.
        restart_turn_started = {
            "type": "agent.turn_started",
            "ts": utc_ts(),
            "service_id": service_id,
            "process_id": process_id,
            "run_id": f"system-restart-{process_id}",
            "reply_index": 0,
            "scope": {"username": scope_username, "session_id": scope_session_id},
        }
        append_user_history(
            runtime_root,
            username=scope_username,
            session_id=scope_session_id,
            entry={
                "direction": "event",
                "ts": utc_ts(),
                "service_id": service_id,
                "event_type": "agent.turn_started",
                "text": f"Agent {service_id} started responding",
                "event": restart_turn_started,
            },
            limit=GOAL_AUDIT_HISTORY_LIMIT,
        )
        append_user_history(
            runtime_root,
            username=scope_username,
            session_id=scope_session_id,
            entry={
                "direction": "session_input",
                "kind": "scheduled_resume" if isinstance(due_auto_resume, dict) else "restart_resume",
                "ts": utc_ts(),
                "service_id": service_id,
                "to": service_id,
                "text": (
                    "自動再開時刻に到達したため、最新 Goal を再開する指示を自分のFIFOへ送りました。"
                    if isinstance(due_auto_resume, dict)
                    else f"システムが再起動しました。前の作業を続けるため、自分のFIFOに継続指示を送りました（process {previous_process_id} → {process_id}）。"
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
    session_id = load_codex_session(
        runtime_root,
        service_id=service_id,
        username=username,
        session_id=session_id,
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
    session_id = load_codex_session(
        runtime_root,
        service_id=service_id,
        username=username,
        session_id=session_id,
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
