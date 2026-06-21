from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runtime.persistent_state_pkg import (
    read_json_file,
    session_goal_manager_reviews_path,
    session_goal_manager_state_path,
)

DEFAULT_RESTART_RESUME_STARTUP_BUDGET = 0
RESTART_RESUME_ONLY_KINDS = {"restart_resume", "scheduled_resume", "turn_completed"}
GOAL_MANAGER_RUNNING_STALE_SECONDS = 120


def restart_resume_startup_budget(self_service: dict[str, Any]) -> int:
    config = self_service.get("config")
    raw_budget: Any = None
    if isinstance(config, dict):
        raw_budget = config.get("restart_resume_startup_budget")
    if raw_budget is None:
        raw_budget = DEFAULT_RESTART_RESUME_STARTUP_BUDGET
    try:
        return max(0, int(raw_budget))
    except (TypeError, ValueError):
        return DEFAULT_RESTART_RESUME_STARTUP_BUDGET


def utc_ts_age_seconds(ts: str) -> float | None:
    text = str(ts or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0.0, datetime.now(UTC).timestamp() - parsed.timestamp())


def build_restart_resume_claim_run_id(
    *,
    restart_generation_id: str,
    scope_session_id: str,
    restart_claim_slot: str,
    service_id: str,
) -> str:
    service_component = "shared" if str(restart_claim_slot or "").strip() == "goal_manager" else service_id
    return (
        f"system-restart-{restart_generation_id}-{scope_session_id}-"
        f"{restart_claim_slot}-{service_component}"
    )


def latest_goal_manager_review(runtime_root: Path, *, username: str, session_id: str) -> dict[str, Any] | None:
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


def history_has_unfinished_turn(history: list[dict[str, Any]]) -> bool:
    last_user_out_ts = ""
    last_turn_completed_ts = ""
    last_turn_activity_ts = ""
    for entry in history:
        ts = str(entry.get("ts") or "")
        direction = str(entry.get("direction") or "")
        event_type = str(entry.get("event_type") or "")
        if direction == "out":
            last_user_out_ts = ts
        if event_type in {"thread.started", "turn.started", "item.started"}:
            if ts >= last_turn_activity_ts:
                last_turn_activity_ts = ts
        if direction == "in" and ts >= last_turn_activity_ts:
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
    return any(
        str(item.get("kind", "")).strip().lower() not in RESTART_RESUME_ONLY_KINDS
        for item in pending_inputs
    )


def has_live_actionable_pending_inputs(pending_inputs: list[dict[str, Any]]) -> bool:
    return has_actionable_pending_inputs(pending_inputs)


def history_has_dangling_goal_audit(history: list[dict[str, Any]]) -> bool:
    last_started_ts = ""
    last_terminal_ts = ""
    for record in history:
        event_type = str(record.get("event_type") or "")
        ts = str(record.get("ts") or "")
        if event_type == "service.goal_manager_compact_started":
            last_started_ts = ts
        elif event_type in {
            "service.goal_manager_compact_completed",
            "service.goal_manager_compact_failed",
            "service.goal_audit_completed",
            "service.goal_audit_failed",
        }:
            last_terminal_ts = ts
    return bool(last_started_ts) and last_started_ts > last_terminal_ts


def history_has_terminal_goal_manager_cycle(history: list[dict[str, Any]]) -> bool:
    last_started_ts = ""
    last_terminal_ts = ""
    for record in history:
        event_type = str(record.get("event_type") or "")
        ts = str(record.get("ts") or "")
        if event_type == "service.goal_manager_compact_started":
            last_started_ts = ts
        elif event_type in {"service.goal_manager_compact_completed", "service.goal_audit_completed"}:
            last_terminal_ts = ts
    return bool(last_terminal_ts) and (not last_started_ts or last_terminal_ts >= last_started_ts)


def latest_goal_manager_failure(history: list[dict[str, Any]]) -> dict[str, Any] | None:
    for record in reversed(history):
        event_type = str(record.get("event_type") or "").strip()
        if event_type in {"service.goal_manager_compact_failed"}:
            return record
        if event_type in {"service.goal_manager_compact_completed", "turn.completed"}:
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


def review_cursor_for_session(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    talk: dict[str, Any],
) -> str:
    cursor = str(talk.get("goal_manager_last_reviewed_turn_completed_at") or "").strip()
    goal_manager_state = read_json_file(
        session_goal_manager_state_path(runtime_root, username=username, session_id=session_id)
    ) or {}
    state_cursor = str(goal_manager_state.get("last_reviewed_turn_completed_at") or "").strip()
    if state_cursor > cursor:
        cursor = state_cursor
    return cursor
