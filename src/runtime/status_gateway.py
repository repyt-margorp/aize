from __future__ import annotations

from typing import Any

from wire.protocol import utc_ts

RUNTIME_STATUS_CHANGED = "runtime.status_changed"
GOAL_STATUS_CHANGED = "goal.status_changed"


def normalize_runtime_execution_state(
    *,
    agent_running: bool = False,
    goal_manager_state: str = "idle",
) -> str:
    gm_state = str(goal_manager_state or "idle").strip().lower()
    if bool(agent_running) or gm_state in {"running", "queued"}:
        return "running"
    if gm_state == "failed":
        return "failed"
    return "idle"


def build_runtime_status(
    *,
    agent_running: bool = False,
    goal_manager_state: str = "idle",
    worker: dict[str, Any] | None = None,
    goal_manager_worker: dict[str, Any] | None = None,
) -> dict[str, Any]:
    runtime_execution_state = normalize_runtime_execution_state(
        agent_running=agent_running,
        goal_manager_state=goal_manager_state,
    )
    return {
        "runtime_execution_state": runtime_execution_state,
        "runtime_in_progress": runtime_execution_state == "running",
        "agent_running": bool(agent_running),
        "goal_manager_state": str(goal_manager_state or "idle").strip().lower() or "idle",
        "worker": worker if isinstance(worker, dict) else None,
        "goal_manager_worker": goal_manager_worker if isinstance(goal_manager_worker, dict) else None,
    }


def merge_runtime_status(summary: dict[str, Any]) -> dict[str, Any]:
    merged = dict(summary)
    status = build_runtime_status(
        agent_running=bool(merged.get("agent_running", False)),
        goal_manager_state=str(merged.get("goal_manager_state") or "idle"),
        worker=merged.get("worker") if isinstance(merged.get("worker"), dict) else None,
        goal_manager_worker=merged.get("goal_manager_worker")
        if isinstance(merged.get("goal_manager_worker"), dict)
        else None,
    )
    merged.update(status)
    return merged


def runtime_status_changed_event(
    *,
    service_id: str,
    username: str,
    session_id: str,
    status: dict[str, Any],
    previous_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event = {
        "type": RUNTIME_STATUS_CHANGED,
        "username": str(username or ""),
        "session_id": str(session_id or ""),
        "runtime_execution_state": str(status.get("runtime_execution_state") or "idle"),
        "runtime_in_progress": bool(status.get("runtime_in_progress", False)),
        "agent_running": bool(status.get("agent_running", False)),
        "goal_manager_state": str(status.get("goal_manager_state") or "idle"),
        "worker": status.get("worker") if isinstance(status.get("worker"), dict) else None,
        "goal_manager_worker": status.get("goal_manager_worker")
        if isinstance(status.get("goal_manager_worker"), dict)
        else None,
    }
    if isinstance(previous_status, dict):
        event["previous_runtime_execution_state"] = str(
            previous_status.get("runtime_execution_state") or "idle"
        )
    state = str(event["runtime_execution_state"])
    text = "Runtime executing" if state == "running" else ("Runtime failed" if state == "failed" else "Runtime idle")
    return {
        "direction": "event",
        "ts": utc_ts(),
        "service_id": str(service_id or ""),
        "event_type": RUNTIME_STATUS_CHANGED,
        "text": text,
        "event": event,
    }


def goal_status_from_session(session: dict[str, Any] | None) -> dict[str, Any]:
    talk = session or {}
    goal_completed = bool(talk.get("goal_completed", False))
    progress_state = str(
        talk.get("goal_progress_state", "complete" if goal_completed else "in_progress")
    ).strip().lower()
    if progress_state not in {"complete", "in_progress"}:
        progress_state = "complete" if goal_completed else "in_progress"
    return {
        "goal_active": bool(talk.get("goal_active", False)),
        "goal_completed": progress_state == "complete",
        "goal_progress_state": progress_state,
        "goal_id": str(talk.get("active_goal_id") or talk.get("goal_id") or ""),
        "goal_text": str(talk.get("goal_text") or ""),
        "goal_completion_policy": str(talk.get("goal_completion_policy") or "standard"),
    }


def goal_status_changed_event(
    *,
    service_id: str,
    username: str,
    session_id: str,
    session: dict[str, Any] | None,
    previous_session: dict[str, Any] | None = None,
) -> dict[str, Any]:
    status = goal_status_from_session(session)
    previous_status = goal_status_from_session(previous_session) if isinstance(previous_session, dict) else None
    event: dict[str, Any] = {
        "type": GOAL_STATUS_CHANGED,
        "username": str(username or ""),
        "session_id": str(session_id or ""),
        **status,
    }
    if previous_status is not None:
        event["previous_goal_progress_state"] = previous_status["goal_progress_state"]
        event["previous_goal_completed"] = previous_status["goal_completed"]
        event["previous_goal_active"] = previous_status["goal_active"]
    progress = str(status["goal_progress_state"])
    text = "Goal completed" if progress == "complete" else "Goal in progress"
    return {
        "direction": "event",
        "ts": utc_ts(),
        "service_id": str(service_id or ""),
        "event_type": GOAL_STATUS_CHANGED,
        "text": text,
        "event": event,
    }
