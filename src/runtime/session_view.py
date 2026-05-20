from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any

from runtime.event_log import make_history_event_entry
from runtime.message_builder import make_aize_pending_input
from runtime.status_gateway import merge_runtime_status
from runtime.persistent_state_pkg import (
    append_history as append_user_history,
    append_pending_input,
    get_history as get_user_history,
    load_pending_inputs,
    read_json_file,
    session_goal_manager_state_path,
    session_service_state_path,
    session_ui_mode,
    write_json_file,
)
from wire.protocol import utc_ts, write_jsonl

GOAL_AUDIT_HISTORY_LIMIT = 500
CANONICAL_LLM_SERVICE_RE = re.compile(r"^service-(codex|claude|gemini)-\d{3}$")


def is_canonical_llm_service_id(service_id: str) -> bool:
    return bool(CANONICAL_LLM_SERVICE_RE.match(str(service_id or "").strip()))


def session_has_active_in_progress_goal(talk: dict[str, Any] | None) -> bool:
    session = talk if isinstance(talk, dict) else {}
    goal_active = bool(session.get("goal_active", True))
    progress = str(
        session.get("goal_progress_state") or ("complete" if bool(session.get("goal_completed", False)) else "in_progress")
    ).strip().lower()
    return goal_active and not bool(session.get("goal_completed", False)) and progress != "complete"


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
            "service.goal_manager_compact_completed",
            "service.goal_manager_compact_failed",
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


def session_agent_assignment_counts(
    talk: dict[str, Any] | None,
    *,
    worker: dict[str, Any] | None = None,
    agent_running: bool | None = None,
    goal_manager_worker: dict[str, Any] | None = None,
    goal_manager_state: str | None = None,
) -> dict[str, int]:
    session = talk if isinstance(talk, dict) else {}
    gm_agents: set[str] = set()
    assigned_agents: set[str] = set()

    def contact_key(item: dict[str, Any]) -> str:
        return str(item.get("service_id") or item.get("agent_id") or "").strip()

    if not session_has_active_in_progress_goal(session):
        return {
            "goal_manager_reviewers": 0,
            "assigned_agents": 0,
        }

    replying = bool(session.get("agent_running", False)) if agent_running is None else bool(agent_running)
    if replying:
        bound_service_id = str(session.get("service_id") or "").strip()
        if bound_service_id:
            assigned_agents.add(bound_service_id)

        if isinstance(worker, dict):
            worker_key = contact_key(worker)
            if worker_key:
                assigned_agents.add(worker_key)

    gm_state = str(goal_manager_state or "").strip().lower()
    if isinstance(goal_manager_worker, dict):
        gm_key = contact_key(goal_manager_worker)
        if gm_key and gm_state in {"running", "queued"}:
            gm_agents.add(gm_key)

    return {
        "goal_manager_reviewers": len(gm_agents),
        "assigned_agents": len(assigned_agents),
    }


def session_assignment_contacts(talk: dict[str, Any] | None) -> list[dict[str, str]]:
    session = talk if isinstance(talk, dict) else {}
    contacts: list[dict[str, str]] = []
    welcomed_agents = session.get("welcomed_agents")
    if not isinstance(welcomed_agents, list):
        return contacts
    for item in welcomed_agents:
        if not isinstance(item, dict):
            continue
        service_id = str(item.get("service_id") or "").strip()
        if not service_id:
            continue
        provider = str(item.get("provider") or "").strip().lower()
        join_role = str(item.get("join_role") or "agent").strip().lower() or "agent"
        contacts.append({
            "service_id": service_id,
            "provider": provider,
            "join_role": join_role,
        })
    return contacts


def session_registration_metadata(
    talk: dict[str, Any] | None,
    *,
    resident_session_ids: set[str] | None = None,
) -> dict[str, Any]:
    session = talk if isinstance(talk, dict) else {}
    session_id = str(session.get("session_id") or "").strip()
    session_group = str(session.get("session_group") or "user").strip().lower() or "user"
    ui_mode = session_ui_mode(session)
    launcher_unit_id = str(session.get("launcher_unit_id") or session.get("launcher_template_id") or "").strip()
    launcher_template_id = str(session.get("launcher_template_id") or launcher_unit_id).strip()
    launcher_unit_kind = str(session.get("launcher_unit_kind") or "").strip().lower()
    launcher_unit_class = str(session.get("launcher_unit_class") or "").strip().lower()
    registered_at = str(session.get("registered_at") or session.get("created_at") or "").strip()
    goal_updated_at = str(
        session.get("goal_updated_at")
        or session.get("updated_at")
        or session.get("created_at")
        or ""
    ).strip()
    resident = session_group in {"root", "unit", "resident", "system"} or ui_mode in {
        "session_map",
        "sessionmap",
        "map_only",
    } or session_id in (resident_session_ids or set()) or (
        bool(launcher_unit_id or launcher_template_id)
        and (
            launcher_unit_kind == "interface"
            or launcher_unit_class == "service"
            or ui_mode == "communication"
        )
    )
    return {
        "registered_at": registered_at,
        "goal_updated_at": goal_updated_at,
        "resident_unit_session": bool(resident),
        "associated_unit_id": launcher_unit_id,
        "associated_template_id": launcher_template_id,
        "associated_unit_display_name": str(session.get("launcher_display_name") or "").strip(),
        "has_associated_unit_file": bool(launcher_unit_id or launcher_template_id),
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
        if event_type in {"service.goal_audit_completed", "service.goal_manager_compact_completed"}:
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
    progress_state = str(state.get("progress_state") or "").strip().lower()
    pending_work_items = (
        list(state.get("pending_work_items", []))
        if isinstance(state.get("pending_work_items"), list)
        else []
    )
    if service_id and progress_state in {"complete", "in_progress"}:
        service_state_path = session_service_state_path(
            runtime_root,
            username=username,
            session_id=session_id,
            service_id=service_id,
        )
        service_state = read_json_file(service_state_path) or {"service_id": service_id}
        goal_manager_state = service_state.get("goal_manager")
        if not isinstance(goal_manager_state, dict):
            goal_manager_state = {}
        current_progress_state = str(goal_manager_state.get("progress_state") or "").strip().lower()
        if current_progress_state != progress_state:
            if progress_state == "complete":
                service_status = "complete"
            elif runtime_state in {"running", "failed", "idle", "queued"}:
                service_status = runtime_state
            else:
                service_status = "in_progress"
            service_state["status"] = service_status
            service_state["updated_at"] = str(state.get("updated_at") or utc_ts())
            service_state["goal_manager"] = {
                "state": runtime_state or "idle",
                "progress_state": progress_state,
                "audit_state": str(state.get("audit_state") or "").strip(),
                "goal_satisfied": bool(state.get("goal_satisfied", False)),
                "summary": str(state.get("summary") or "").strip(),
                "pending_work_items": pending_work_items,
                "updated_at": str(state.get("updated_at") or utc_ts()),
            }
            write_json_file(service_state_path, service_state)
    if runtime_state == "running":
        return {"state": "running", "service_id": service_id, "pending_work_items": pending_work_items}
    if runtime_state == "queued" and pending_work_items:
        return {"state": "queued", "service_id": service_id, "pending_work_items": pending_work_items}
    if runtime_state == "failed":
        return {"state": "failed", "service_id": service_id}
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
    resident_session_ids: set[str] | None = None,
) -> dict[str, Any]:
    session_id = str(talk.get("session_id") or "")
    preferred_provider = str(talk.get("preferred_provider", default_provider)).strip().lower() or default_provider
    bound_service_id = str(talk.get("service_id") or "").strip()
    active_turn = active_agent_turn_state(history_entries)
    active_service_id = str((active_turn or {}).get("service_id") or "").strip()
    active_started_ts = str((active_turn or {}).get("started_ts") or "").strip()
    has_followup_activity = bool(
        active_started_ts
        and any(str(entry.get("ts") or "").strip() > active_started_ts for entry in history_entries)
    )
    agent_running = bool(active_turn) and has_followup_activity
    visible_worker = worker_slot_badge(
        active_service_id if agent_running else bound_service_id,
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
    agent_counts = session_agent_assignment_counts(
        talk,
        worker=visible_worker,
        agent_running=agent_running,
        goal_manager_worker=goal_manager_worker,
        goal_manager_state=str(goal_manager_state.get("state") or "idle"),
    )
    user_response_wait_active = bool(talk.get("user_response_wait_active", False))
    user_response_wait_started_at = str(talk.get("user_response_wait_started_at", "") or "")
    user_response_wait_generated_at = str(talk.get("user_response_wait_generated_at", "") or "")
    user_response_wait_requests_raw = (
        talk.get("user_response_wait_requests")
        if isinstance(talk.get("user_response_wait_requests"), list)
        else []
    )
    user_response_wait_requests = [
        {
            "request_id": str(item.get("request_id") or "").strip(),
            "generated_at": str(item.get("generated_at") or "").strip(),
            "started_at": str(item.get("started_at") or "").strip(),
            "until_at": str(item.get("until_at") or "").strip(),
            "timeout_seconds": int(item.get("timeout_seconds", 300) or 300),
            "effective_timeout_seconds": int(item.get("effective_timeout_seconds", 300) or 300),
            "question": str(item.get("question") or "").strip(),
            "reason": str(item.get("reason") or "").strip(),
            "source_service_id": str(item.get("source_service_id") or "").strip(),
            "requested_by_role": str(item.get("requested_by_role") or "").strip(),
            "status": str(item.get("status") or "").strip(),
            "cleared_at": str(item.get("cleared_at") or "").strip(),
            "answered_by_user": bool(item.get("answered_by_user", False)),
        }
        for item in user_response_wait_requests_raw
        if isinstance(item, dict)
    ]
    user_response_wait_status = (
        "waiting"
        if user_response_wait_active
        else (
            "timed_out"
            if str(talk.get("user_response_wait_last_timeout_at", "") or "").strip()
            else ("recorded" if user_response_wait_started_at or user_response_wait_generated_at else "idle")
        )
    )
    goal_text = str(talk.get("goal_text", "")).strip()
    goal_completed = bool(talk.get("goal_completed", False))
    goal_progress_state = str(
        talk.get("goal_progress_state", "complete" if goal_completed else "in_progress")
    ).strip().lower()
    return merge_runtime_status({
        "session_id": session_id,
        "label": str(talk.get("label", session_id)),
        "created_at": str(talk.get("created_at", "") or ""),
        "updated_at": str(talk.get("updated_at", "") or ""),
        **session_registration_metadata(talk, resident_session_ids=resident_session_ids),
        "goal_text": goal_text,
        "goal_active": bool(talk.get("goal_active", False)),
        "goal_completed": goal_completed,
        "goal_progress_state": goal_progress_state,
        "goal_audit_state": str(talk.get("goal_audit_state", "all_clear") or "all_clear"),
        "agent_welcome_enabled": bool(talk.get("agent_welcome_enabled", False)),
        "preferred_provider": preferred_provider,
        "bound_service_id": bound_service_id,
        "worker": visible_worker,
        "agent_running": agent_running,
        "goal_manager_state": str(goal_manager_state.get("state") or "idle"),
        "goal_manager_provider": goal_manager_provider,
        "goal_manager_worker": goal_manager_worker,
        "agent_contacts": session_assignment_contacts(talk),
        "agent_counts": agent_counts,
        "goal_manager_reviewer_count": agent_counts["goal_manager_reviewers"],
        "assigned_agent_count": agent_counts["assigned_agents"],
        "auto_resume_enabled": bool(talk.get("auto_resume_enabled", False)),
        "user_response_wait_status": user_response_wait_status,
        "user_response_wait_active": user_response_wait_active,
        "user_response_wait_timeout_seconds": int(talk.get("user_response_wait_timeout_seconds", 300) or 300),
        "user_response_wait_effective_timeout_seconds": int(
            talk.get("user_response_wait_effective_timeout_seconds", 300) or 300
        ),
        "user_response_wait_started_at": user_response_wait_started_at,
        "user_response_wait_generated_at": user_response_wait_generated_at,
        "user_response_wait_until_at": str(talk.get("user_response_wait_until_at", "") or ""),
        "user_response_wait_request_id": str(talk.get("user_response_wait_request_id", "") or ""),
        "user_response_wait_prompt_text": str(talk.get("user_response_wait_prompt_text", "") or "").strip(),
        "user_response_wait_reason": str(talk.get("user_response_wait_reason", "") or "").strip(),
        "user_response_wait_requests": user_response_wait_requests,
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
        "child_session_sharing": dict(talk.get("child_session_sharing", {}))
        if isinstance(talk.get("child_session_sharing"), dict)
        else {},
    })


def build_worker_count_summary(
    *,
    service_snapshots: list[dict[str, Any]],
    session_summaries: list[dict[str, Any]],
) -> dict[str, dict[str, int]]:
    counts = {
        "codex": {"running": 0, "assigned_slots": 0, "goal_manager_reviewers": 0, "active_turns": 0, "replying_turns": 0, "reviewing_turns": 0},
        "claude": {"running": 0, "assigned_slots": 0, "goal_manager_reviewers": 0, "active_turns": 0, "replying_turns": 0, "reviewing_turns": 0},
        "gemini": {"running": 0, "assigned_slots": 0, "goal_manager_reviewers": 0, "active_turns": 0, "replying_turns": 0, "reviewing_turns": 0},
    }
    assigned_slots: dict[str, set[str]] = {
        "codex": set(),
        "claude": set(),
        "gemini": set(),
    }
    goal_manager_reviewers: dict[str, set[str]] = {
        "codex": set(),
        "claude": set(),
        "gemini": set(),
    }
    for snapshot in service_snapshots:
        service = snapshot.get("service") if isinstance(snapshot, dict) else None
        process = snapshot.get("process") if isinstance(snapshot, dict) else None
        if not isinstance(service, dict):
            continue
        kind = str(service.get("kind") or "").strip().lower()
        if kind not in counts:
            continue
        service_id = str(service.get("service_id") or "").strip()
        if not is_canonical_llm_service_id(service_id):
            continue
        status = str((process or {}).get("status") or service.get("status") or "").strip().lower()
        if status and status != "stopped":
            counts[kind]["running"] += 1
    for talk in session_summaries:
        if not isinstance(talk, dict):
            continue
        if not session_has_active_in_progress_goal(talk):
            continue
        def resolve_provider(candidate: dict[str, Any] | None) -> str:
            worker = candidate if isinstance(candidate, dict) else {}
            provider = str(worker.get("provider") or "").strip().lower()
            service_id = str(worker.get("service_id") or "").strip().lower()
            if provider not in counts and service_id:
                if "claude" in service_id:
                    provider = "claude"
                elif "codex" in service_id:
                    provider = "codex"
                elif "gemini" in service_id:
                    provider = "gemini"
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

        def track_assignment(candidate: dict[str, Any] | None = None, *, fallback_service_id: str = "") -> None:
            provider = resolve_provider(candidate)
            service_id = ""
            if isinstance(candidate, dict):
                service_id = str(candidate.get("service_id") or "").strip().lower()
            if not service_id:
                service_id = str(fallback_service_id or "").strip().lower()
            if provider in assigned_slots and service_id:
                assigned_slots[provider].add(service_id)

        def track_welcomed_assignment(item: dict[str, Any]) -> None:
            service_id = str(item.get("service_id") or "").strip().lower()
            if not service_id:
                return
            role = str(item.get("join_role") or "agent").strip().lower() or "agent"
            provider = str(item.get("provider") or "").strip().lower()
            if provider not in counts:
                provider = resolve_provider({"service_id": service_id})
            if provider not in counts:
                return
            if role == "goal_manager":
                goal_manager_reviewers[provider].add(service_id)
            else:
                assigned_slots[provider].add(service_id)

        assignment_contacts = talk.get("agent_contacts") if isinstance(talk.get("agent_contacts"), list) else []
        if not assignment_contacts:
            assignment_contacts = talk.get("welcomed_agents") if isinstance(talk.get("welcomed_agents"), list) else []
        for item in assignment_contacts:
            if isinstance(item, dict):
                track_welcomed_assignment(item)

        if talk.get("agent_running"):
            track_assignment(
                talk.get("worker") if isinstance(talk.get("worker"), dict) else None,
                fallback_service_id=str(talk.get("bound_service_id") or ""),
            )
        goal_manager_worker = talk.get("goal_manager_worker") if isinstance(talk.get("goal_manager_worker"), dict) else None
        goal_manager_service_id = str((goal_manager_worker or {}).get("service_id") or "").strip().lower()

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
                reviewer_service_id = goal_manager_service_id or str(talk.get("bound_service_id") or "").strip().lower()
                if reviewer_service_id:
                    assigned_slots[provider].add(reviewer_service_id)
                    goal_manager_reviewers[provider].add(reviewer_service_id)
                counts[provider]["reviewing_turns"] += 1
        elif str(talk.get("goal_manager_state") or "").strip().lower() == "queued":
            pending_work_items = (
                talk.get("goal_manager_pending_work_items")
                if isinstance(talk.get("goal_manager_pending_work_items"), list)
                else []
            )
            pending_reviewers: set[tuple[str, str]] = set()
            for item in pending_work_items:
                if not isinstance(item, dict):
                    continue
                service_id = str(item.get("service_id") or goal_manager_service_id).strip().lower()
                provider = resolve_provider({"service_id": service_id, "provider": item.get("provider", "")})
                if provider in counts and service_id:
                    pending_reviewers.add((provider, service_id))
            if not pending_reviewers:
                provider = resolve_provider(goal_manager_worker)
                if provider in counts and goal_manager_service_id:
                    pending_reviewers.add((provider, goal_manager_service_id))
            for provider, service_id in pending_reviewers:
                assigned_slots[provider].add(service_id)
                goal_manager_reviewers[provider].add(service_id)
                counts[provider]["reviewing_turns"] += 1
    for provider, service_ids in assigned_slots.items():
        counts[provider]["assigned_slots"] = len(service_ids)
        counts[provider]["goal_manager_reviewers"] = len(goal_manager_reviewers[provider])
        counts[provider]["active_turns"] = counts[provider]["replying_turns"] + counts[provider]["reviewing_turns"]
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
