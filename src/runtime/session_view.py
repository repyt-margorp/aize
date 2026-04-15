from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from runtime.event_log import make_history_event_entry
from runtime.message_builder import make_aize_pending_input
from runtime.persistent_state import (
    append_history as append_user_history,
    append_pending_input,
    get_history as get_user_history,
    load_pending_inputs,
    read_json_file,
    session_goal_manager_state_path,
    session_ui_mode,
)
from wire.protocol import utc_ts, write_jsonl

GOAL_AUDIT_HISTORY_LIMIT = 500


def active_agent_turn_state(history_entries: list[dict[str, Any]]) -> dict[str, str] | None:
    active_service_id = ""
    active_started_ts = ""
    for entry in history_entries:
        event_type = str(entry.get("event_type") or "")
        ts = str(entry.get("ts") or "")
        if event_type == "agent.turn_started":
            event = entry.get("event") if isinstance(entry.get("event"), dict) else {}
            # GoalManager reviews have their own runtime lane and should not make the
            # session look like a user-visible agent reply is still in progress.
            if bool(event.get("goal_manager")):
                active_service_id = ""
                active_started_ts = ""
                continue
            active_service_id = str(entry.get("service_id") or entry.get("from") or "").strip()
            active_started_ts = ts
            continue
        if not active_started_ts:
            continue
        if event_type == "turn.completed":
            completed_ts = ts
            if completed_ts >= active_started_ts:
                active_service_id = ""
                active_started_ts = ""
                continue
        if event_type in {
            "service.goal_audit_completed",
            "service.goal_audit_failed",
            "service.goal_manager_reset",
            "service.goal_manager_compact_failed",
            "service.post_turn_followup_failed",
        }:
            event_service_id = str(entry.get("service_id") or entry.get("from") or "").strip()
            if not event_service_id or event_service_id == active_service_id:
                active_service_id = ""
                active_started_ts = ""
                continue
        if str(entry.get("direction") or "") == "in":
            reply_ts = ts
            if reply_ts >= active_started_ts:
                active_service_id = ""
                active_started_ts = ""
    if not active_service_id or not active_started_ts:
        return None
    return {"service_id": active_service_id, "started_ts": active_started_ts}


def worker_slot_badge(
    service_id: str | None,
    *,
    codex_service_pool: list[str],
    claude_service_pool: list[str],
    gemini_service_pool: list[str],
) -> dict[str, Any] | None:
    normalized_service_id = str(service_id or "").strip()
    if not normalized_service_id:
        return None
    if normalized_service_id in codex_service_pool:
        return {
            "service_id": normalized_service_id,
            "provider": "codex",
            "slot": codex_service_pool.index(normalized_service_id) + 1,
        }
    if normalized_service_id in claude_service_pool:
        return {
            "service_id": normalized_service_id,
            "provider": "claude",
            "slot": claude_service_pool.index(normalized_service_id) + 1,
        }
    if normalized_service_id in gemini_service_pool:
        return {
            "service_id": normalized_service_id,
            "provider": "gemini",
            "slot": gemini_service_pool.index(normalized_service_id) + 1,
        }
    return {
        "service_id": normalized_service_id,
        "provider": "unknown",
        "slot": None,
    }


def latest_goal_manager_runtime_state(history_entries: list[dict[str, Any]]) -> dict[str, Any]:
    for entry in sorted(history_entries, key=lambda item: str(item.get("ts") or ""), reverse=True):
        event_type = str(entry.get("event_type") or "")
        service_id = str(entry.get("service_id") or entry.get("from") or "").strip()
        if event_type in {
            "service.post_turn_followup_started",
            "service.goal_audit_started",
            "service.goal_manager_compact_started",
        }:
            return {"state": "running", "service_id": service_id}
        if event_type == "service.goal_manager_reset":
            return {"state": "idle", "service_id": ""}
        if event_type == "service.goal_audit_completed":
            event = entry.get("event") if isinstance(entry.get("event"), dict) else {}
            progress_state = str(
                event.get("progress_state", "complete" if bool(event.get("goal_satisfied")) else "in_progress")
            ).strip().lower()
            return {
                "state": "complete" if progress_state == "complete" else "waiting",
                "service_id": service_id,
            }
        if event_type in {
            "service.goal_audit_failed",
            "service.goal_manager_compact_failed",
            "service.post_turn_followup_failed",
        }:
            return {"state": "failed", "service_id": service_id}
    return {"state": "idle", "service_id": ""}


def persisted_goal_manager_runtime_state(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    bound_service_id: str = "",
) -> dict[str, Any]:
    state = read_json_file(
        session_goal_manager_state_path(runtime_root, username=username, session_id=session_id)
    ) or {}
    service_id = str(state.get("service_id") or bound_service_id or "").strip()
    runtime_state = str(state.get("state") or "idle").strip().lower()
    if runtime_state == "running":
        return {"state": "running", "service_id": service_id}
    if runtime_state == "failed":
        return {"state": "failed", "service_id": service_id}
    progress_state = str(state.get("progress_state") or "").strip().lower()
    if progress_state == "complete":
        return {"state": "complete", "service_id": service_id}
    if progress_state == "in_progress":
        return {"state": "waiting", "service_id": service_id}
    return {"state": "idle", "service_id": service_id}


def build_session_runtime_summary(
    talk: dict[str, Any],
    *,
    history_entries: list[dict[str, Any]],
    codex_service_pool: list[str],
    claude_service_pool: list[str],
    gemini_service_pool: list[str],
    default_provider: str,
) -> dict[str, Any]:
    session_id = str(talk.get("session_id") or "")
    preferred_provider = str(talk.get("preferred_provider", default_provider)).strip().lower() or default_provider
    bound_service_id = str(talk.get("service_id") or "").strip()
    visible_worker = worker_slot_badge(
        bound_service_id,
        codex_service_pool=codex_service_pool,
        claude_service_pool=claude_service_pool,
        gemini_service_pool=gemini_service_pool,
    )
    goal_manager_state = latest_goal_manager_runtime_state(history_entries)
    goal_manager_worker = worker_slot_badge(
        str(goal_manager_state.get("service_id") or bound_service_id),
        codex_service_pool=codex_service_pool,
        claude_service_pool=claude_service_pool,
        gemini_service_pool=gemini_service_pool,
    )
    goal_manager_provider = str(
        (goal_manager_worker or {}).get("provider") or preferred_provider or default_provider
    ).strip().lower() or default_provider
    user_response_wait_active = bool(talk.get("user_response_wait_active", False))
    user_response_wait_started_at = str(talk.get("user_response_wait_started_at", "") or "")
    user_response_wait_status = (
        "waiting"
        if user_response_wait_active
        else (
            "timed_out"
            if str(talk.get("user_response_wait_last_timeout_at", "") or "").strip()
            else ("recorded" if user_response_wait_started_at else "idle")
        )
    )
    goal_text = str(talk.get("goal_text", "")).strip()
    goal_completed = bool(talk.get("goal_completed", False))
    goal_progress_state = str(
        talk.get("goal_progress_state", "complete" if goal_completed else "in_progress")
    ).strip().lower()
    return {
        "session_id": session_id,
        "label": str(talk.get("label", session_id)),
        "goal_text": goal_text,
        "goal_active": bool(talk.get("goal_active", False)),
        "goal_completed": goal_completed,
        "goal_progress_state": goal_progress_state,
        "agent_welcome_enabled": bool(talk.get("agent_welcome_enabled", False)),
        "preferred_provider": preferred_provider,
        "bound_service_id": bound_service_id,
        "worker": visible_worker,
        "agent_running": False,
        "goal_manager_state": str(goal_manager_state.get("state") or "idle"),
        "goal_manager_provider": goal_manager_provider,
        "goal_manager_worker": goal_manager_worker,
        "auto_resume_enabled": bool(talk.get("auto_resume_enabled", False)),
        "user_response_wait_status": user_response_wait_status,
        "user_response_wait_active": user_response_wait_active,
        "user_response_wait_started_at": user_response_wait_started_at,
        "user_response_wait_until_at": str(talk.get("user_response_wait_until_at", "") or ""),
        "user_response_wait_prompt_text": str(talk.get("user_response_wait_prompt_text", "") or "").strip(),
        "parent_session_id": str(talk.get("parent_session_id") or "").strip(),
        "created_by_username": str(talk.get("created_by_username") or "").strip(),
        "created_by_type": str(talk.get("created_by_type") or "").strip(),
        "origin_session_id": str(talk.get("origin_session_id") or "").strip(),
        "origin_goal_id": str(talk.get("origin_goal_id") or "").strip(),
        "session_group": str(talk.get("session_group") or "user").strip().lower() or "user",
        "session_ui_mode": session_ui_mode(talk),
        "session_permissions": dict(talk.get("session_permissions", {}))
        if isinstance(talk.get("session_permissions"), dict)
        else {},
    }


def build_worker_count_summary(
    *,
    service_snapshots: list[dict[str, Any]],
    session_summaries: list[dict[str, Any]],
) -> dict[str, dict[str, int]]:
    counts = {
        "codex": {"running": 0, "active_turns": 0, "replying_turns": 0, "reviewing_turns": 0},
        "claude": {"running": 0, "active_turns": 0, "replying_turns": 0, "reviewing_turns": 0},
        "gemini": {"running": 0, "active_turns": 0, "replying_turns": 0, "reviewing_turns": 0},
    }
    for snapshot in service_snapshots:
        service = snapshot.get("service") if isinstance(snapshot, dict) else None
        process = snapshot.get("process") if isinstance(snapshot, dict) else None
        if not isinstance(service, dict):
            continue
        kind = str(service.get("kind") or "").strip().lower()
        if kind not in counts:
            continue
        status = str((process or {}).get("status") or service.get("status") or "").strip().lower()
        if status and status != "stopped":
            counts[kind]["running"] += 1
    for talk in session_summaries:
        if not isinstance(talk, dict):
            continue
        def resolve_provider(candidate: dict[str, Any] | None) -> str:
            worker = candidate if isinstance(candidate, dict) else {}
            provider = str(worker.get("provider") or "").strip().lower()
            if provider not in counts:
                provider = str(talk.get("preferred_provider") or "").strip().lower()
            if provider not in counts:
                bound_service_id = str(talk.get("bound_service_id") or "").strip().lower()
                if "claude" in bound_service_id:
                    provider = "claude"
                elif "codex" in bound_service_id:
                    provider = "codex"
                elif "gemini" in bound_service_id:
                    provider = "gemini"
            return provider

        if talk.get("agent_running"):
            provider = resolve_provider(talk.get("worker") if isinstance(talk.get("worker"), dict) else None)
            if provider in counts:
                counts[provider]["active_turns"] += 1
                counts[provider]["replying_turns"] += 1

        if str(talk.get("goal_manager_state") or "").strip().lower() == "running":
            provider = resolve_provider(
                talk.get("goal_manager_worker") if isinstance(talk.get("goal_manager_worker"), dict) else None
            )
            if provider in counts:
                counts[provider]["active_turns"] += 1
                counts[provider]["reviewing_turns"] += 1
    return counts


def pending_progress_inquiry_exists(
    pending_inputs: list[dict[str, Any]],
    *,
    service_id: str,
) -> bool:
    for item in pending_inputs:
        if str(item.get("kind") or "") != "progress_inquiry":
            continue
        text = str(item.get("text") or "")
        if f"<service_id>{html.escape(service_id)}</service_id>" in text:
            return True
    return False


def build_progress_inquiry_xml(
    *,
    service_id: str,
    source_kind: str,
    source_text: str,
) -> str:
    trimmed_text = source_text.strip()
    return "\n".join(
        [
            "<aize_progress_inquiry>",
            f"  <service_id>{html.escape(service_id)}</service_id>",
            f"  <source_kind>{html.escape(source_kind)}</source_kind>",
            "  <instruction>While you were responding, new FIFO input arrived. Begin your next turn by stating your latest concrete progress, then address the queued inputs.</instruction>",
            f"  <latest_fifo_text>{html.escape(trimmed_text)}</latest_fifo_text>",
            "</aize_progress_inquiry>",
        ]
    )


def maybe_enqueue_mid_turn_progress_inquiry(
    *,
    runtime_root: Path,
    log_path: Path,
    http_service_id: str,
    process_id: str,
    username: str,
    session_id: str,
    source_kind: str,
    source_text: str,
    provider: str,
) -> bool:
    history = get_user_history(runtime_root, username=username, session_id=session_id)
    active_turn = active_agent_turn_state(history)
    if not active_turn:
        return False
    active_service_id = str(active_turn.get("service_id") or "").strip()
    if not active_service_id:
        return False
    pending_inputs = load_pending_inputs(runtime_root, username=username, session_id=session_id)
    if pending_progress_inquiry_exists(pending_inputs, service_id=active_service_id):
        return False

    append_pending_input(
        runtime_root,
        username=username,
        session_id=session_id,
        entry=make_aize_pending_input(
            kind="progress_inquiry",
            role="system",
            text=build_progress_inquiry_xml(
                service_id=active_service_id,
                source_kind=source_kind,
                source_text=source_text,
            ),
        ),
    )
    requested_event = {
        "type": "service.progress_inquiry_requested",
        "service_id": active_service_id,
        "session_id": session_id,
        "source_kind": source_kind,
        "status": "queued",
    }
    deferred_event = {
        "type": "service.progress_inquiry_deferred",
        "service_id": active_service_id,
        "session_id": session_id,
        "provider": provider,
        "fallback": "deferred_until_turn_completed",
        "reason": "live_progress_inquiry_unsupported",
    }
    for event in (requested_event, deferred_event):
        write_jsonl(
            log_path,
            {
                "type": event["type"],
                "ts": utc_ts(),
                "service_id": http_service_id,
                "process_id": process_id,
                "scope": {"username": username, "session_id": session_id},
                "event": event,
            },
        )
        append_user_history(
            runtime_root,
            username=username,
            session_id=session_id,
            entry=make_history_event_entry(event, service_id=active_service_id),
            limit=GOAL_AUDIT_HISTORY_LIMIT,
        )
    return True
