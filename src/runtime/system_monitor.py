from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kernel.lifecycle import load_lifecycle_state
from kernel.registry import load_registry
from runtime.persistent_state_pkg import (
    get_history,
    list_all_sessions_with_users,
    load_goal_manager_pending_inputs,
    load_pending_inputs,
)
from runtime.session_view import active_agent_turn_state


USER_INPUT_KINDS = {"user_message", "user_response"}
TERMINAL_PROCESS_STATUSES = {"stopped", "failed", "crashed", "dead", "exited"}
TERMINAL_SERVICE_STATUSES = {"stopped", "failed", "crashed", "dead", "exited"}


def _parse_utc_ts(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)
        return datetime.fromisoformat(text).astimezone(UTC)
    except ValueError:
        return None


def _age_seconds(value: Any, *, now: datetime) -> int | None:
    parsed = _parse_utc_ts(value)
    if parsed is None:
        return None
    return max(0, int((now - parsed).total_seconds()))


def _completed_session_masks_stale_turn(
    *,
    session: dict[str, Any],
    active_turn: dict[str, Any] | None,
    pending_user_inputs: list[dict[str, Any]],
    goal_manager_inputs: list[dict[str, Any]],
) -> bool:
    if not active_turn:
        return False
    if bool(session.get("user_response_wait_active", False)):
        return False
    if pending_user_inputs or goal_manager_inputs:
        return False
    if not bool(session.get("goal_active", False)):
        return False
    goal_completed = bool(session.get("goal_completed", False))
    goal_progress_state = str(
        session.get("goal_progress_state", "complete" if goal_completed else "in_progress")
    ).strip().lower()
    if goal_progress_state != "complete":
        return False
    active_started_at = _parse_utc_ts((active_turn or {}).get("started_ts"))
    updated_at = _parse_utc_ts(session.get("updated_at"))
    if active_started_at is None or updated_at is None:
        return False
    return updated_at >= active_started_at


def _service_problem(
    *,
    service_id: str,
    registry_services: dict[str, Any],
    lifecycle_processes: dict[str, Any],
) -> str:
    if not service_id:
        return ""
    service = registry_services.get(service_id)
    if not isinstance(service, dict):
        return "bound_service_missing_from_registry"
    service_status = str(service.get("status") or "").strip().lower()
    if service_status in TERMINAL_SERVICE_STATUSES:
        return f"bound_service_status:{service_status}"
    process_id = str(service.get("current_process_id") or "").strip()
    if not process_id:
        return ""
    process = lifecycle_processes.get(process_id)
    if not isinstance(process, dict):
        return "bound_service_process_missing"
    process_status = str(process.get("status") or "").strip().lower()
    if process_status in TERMINAL_PROCESS_STATUSES:
        return f"bound_service_process_status:{process_status}"
    return ""


def scan_system_sessions(
    runtime_root: Path,
    *,
    now: datetime | None = None,
    stalled_after_seconds: int = 60 * 60,
) -> dict[str, Any]:
    effective_now = (now or datetime.now(UTC)).astimezone(UTC)
    try:
        registry_services = load_registry(runtime_root).get("services", {})
    except Exception:
        registry_services = {}
    try:
        lifecycle_processes = load_lifecycle_state(runtime_root).get("processes", {})
    except Exception:
        lifecycle_processes = {}
    if not isinstance(registry_services, dict):
        registry_services = {}
    if not isinstance(lifecycle_processes, dict):
        lifecycle_processes = {}

    findings: list[dict[str, Any]] = []
    sessions = list_all_sessions_with_users(runtime_root)
    for session in sessions:
        username = str(session.get("username") or "").strip()
        session_id = str(session.get("session_id") or "").strip()
        if not username or not session_id:
            continue
        history_entries = get_history(runtime_root, username=username, session_id=session_id)
        pending_inputs = load_pending_inputs(runtime_root, username=username, session_id=session_id)
        goal_manager_inputs = load_goal_manager_pending_inputs(
            runtime_root,
            username=username,
            session_id=session_id,
        )
        pending_user_inputs = [
            item for item in pending_inputs if str(item.get("kind") or "").strip().lower() in USER_INPUT_KINDS
        ]
        goal_active = bool(session.get("goal_active", False))
        goal_progress_state = str(session.get("goal_progress_state") or "").strip().lower()
        unfinished_goal = goal_active and goal_progress_state != "complete"
        user_wait_active = bool(session.get("user_response_wait_active", False))
        active_turn = active_agent_turn_state(history_entries)
        active_turn_age = _age_seconds((active_turn or {}).get("started_ts"), now=effective_now)
        updated_age = _age_seconds(session.get("updated_at"), now=effective_now)
        service_id = str(session.get("service_id") or "").strip()
        service_problem = _service_problem(
            service_id=service_id,
            registry_services=registry_services,
            lifecycle_processes=lifecycle_processes,
        )
        stalled_reasons: list[str] = []
        stale_turn_ignored = _completed_session_masks_stale_turn(
            session=session,
            active_turn=active_turn,
            pending_user_inputs=pending_user_inputs,
            goal_manager_inputs=goal_manager_inputs,
        )
        if (
            active_turn
            and active_turn_age is not None
            and active_turn_age >= stalled_after_seconds
            and not stale_turn_ignored
        ):
            stalled_reasons.append("agent_turn_exceeded_threshold")
        if (
            unfinished_goal
            and not user_wait_active
            and not pending_user_inputs
            and updated_age is not None
            and updated_age >= stalled_after_seconds
        ):
            stalled_reasons.append("unfinished_goal_without_recent_session_update")
        if unfinished_goal or user_wait_active or pending_user_inputs or stalled_reasons or service_problem:
            findings.append(
                {
                    "username": username,
                    "session_id": session_id,
                    "label": str(session.get("label") or "").strip(),
                    "parent_session_id": str(session.get("parent_session_id") or "").strip(),
                    "goal_active": goal_active,
                    "goal_progress_state": goal_progress_state or ("in_progress" if unfinished_goal else "complete"),
                    "unfinished_goal": unfinished_goal,
                    "unresolved_user_input": bool(user_wait_active or pending_user_inputs),
                    "pending_user_input_count": len(pending_user_inputs),
                    "goal_manager_pending_input_count": len(goal_manager_inputs),
                    "user_response_wait_active": user_wait_active,
                    "stalled": bool(stalled_reasons),
                    "stalled_reasons": stalled_reasons,
                    "active_service_id": str((active_turn or {}).get("service_id") or service_id),
                    "active_turn_age_seconds": active_turn_age,
                    "updated_age_seconds": updated_age,
                    "system_problem": service_problem,
                }
            )
    counts = {
        "sessions_scanned": len(sessions),
        "findings": len(findings),
        "unresolved_user_input": sum(1 for item in findings if item["unresolved_user_input"]),
        "unfinished_goals": sum(1 for item in findings if item["unfinished_goal"]),
        "stalled": sum(1 for item in findings if item["stalled"]),
        "system_problems": sum(1 for item in findings if item["system_problem"]),
    }
    return {
        "kind": "aize_system_monitor_scan",
        "generated_at": effective_now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "stalled_after_seconds": stalled_after_seconds,
        "counts": counts,
        "findings": findings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan AIze sessions for unresolved or stalled work.")
    parser.add_argument("--runtime-root", default=".aize-runtime")
    parser.add_argument("--stalled-after-seconds", type=int, default=60 * 60)
    parser.add_argument("--json", action="store_true", help="Emit the scan as JSON.")
    args = parser.parse_args()
    report = scan_system_sessions(
        Path(args.runtime_root),
        stalled_after_seconds=max(60, int(args.stalled_after_seconds)),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.json else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
