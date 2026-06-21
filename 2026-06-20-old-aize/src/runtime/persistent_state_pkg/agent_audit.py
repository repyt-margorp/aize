from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from wire.protocol import utc_ts

from ._core import (
    normalize_username,
    read_json_file,
    read_jsonl,
    remove_file_if_exists,
    session_metadata_path,
    session_agent_state_path,
    session_goal_manager_state_path,
    session_pending_dir,
    session_services_dir,
    state_lock,
    state_read_lock,
    write_json_file,
)

AUDIT_STATE_RANK = {"all_clear": 0, "needs_compact": 1, "panic": 2}
CANONICAL_LLM_SERVICE_RE = re.compile(r"^service-(codex|claude|gemini)-\d{3}$")
_STALE_GOAL_MANAGER_PENDING_KINDS = {"turn_completed", "lifecycle_owner_lost"}
_TERMINAL_SERVICE_STATES = {"failed", "stopped", "crashed", "dead", "exited"}


def _service_pending_paths(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    service_id: str,
) -> list[Path]:
    services_pending_dir = session_pending_dir(
        runtime_root,
        username=username,
        session_id=session_id,
    ) / "services"
    paths: list[Path] = []
    legacy_path = services_pending_dir / f"{service_id}.jsonl"
    if legacy_path.exists():
        paths.append(legacy_path)
    for path in sorted(services_pending_dir.glob(f"{service_id}@@*.jsonl")):
        if path not in paths:
            paths.append(path)
    return paths


def normalize_audit_state(value: object) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in AUDIT_STATE_RANK else "all_clear"


def load_agent_audit_state(
    runtime_root: Path,
    *,
    service_id: str,
    username: str,
    session_id: str,
) -> str:
    """Load the audit state for an agent-talk binding.

    Returns one of ``all_clear``, ``needs_compact``, or ``panic``.
    """
    with state_read_lock(runtime_root):
        file_record = read_json_file(
            session_agent_state_path(
                runtime_root,
                username=username,
                session_id=session_id,
                service_id=service_id,
            )
        )
        if isinstance(file_record, dict):
            return normalize_audit_state(file_record.get("audit_state", "all_clear"))
        return "all_clear"


def reconcile_stale_session_service_states(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    preferred_service_id: str | None = None,
) -> list[str]:
    normalized = normalize_username(username)
    session_path = session_metadata_path(runtime_root, username=normalized, session_id=session_id)
    reconciled: list[str] = []
    with state_lock(runtime_root):
        goal_manager_state = read_json_file(
            session_goal_manager_state_path(runtime_root, username=normalized, session_id=session_id)
        )
        if not isinstance(goal_manager_state, dict):
            return reconciled
        runtime_state = str(goal_manager_state.get("state") or "").strip().lower()
        progress_state = str(goal_manager_state.get("progress_state") or "").strip().lower()
        pending_work_items = (
            list(goal_manager_state.get("pending_work_items", []))
            if isinstance(goal_manager_state.get("pending_work_items"), list)
            else []
        )
        if progress_state not in {"in_progress", "complete"}:
            return reconciled
        if runtime_state not in {"", "idle", "complete"}:
            return reconciled
        if pending_work_items:
            return reconciled

        session_record = read_json_file(session_path) or {}
        keep_service_ids = {
            service_id
            for service_id in (
                str(goal_manager_state.get("service_id") or "").strip(),
                str(session_record.get("service_id") or "").strip(),
                str(preferred_service_id or "").strip(),
            )
            if service_id
        }
        target_state = "complete" if progress_state == "complete" else "idle"
        services_dir = session_services_dir(runtime_root, username=normalized, session_id=session_id)
        for service_state_path in sorted(services_dir.glob("*.json")):
            if service_state_path.name.endswith(".audit.json"):
                continue
            service_state = read_json_file(service_state_path)
            if not isinstance(service_state, dict):
                continue
            service_id = str(service_state.get("service_id") or service_state_path.stem).strip()
            if not service_id or service_id in keep_service_ids:
                continue
            status = str(service_state.get("status") or "").strip().lower()
            goal_manager = service_state.get("goal_manager")
            if not isinstance(goal_manager, dict):
                goal_manager = {}
            snapshot_state = str(goal_manager.get("state") or "").strip().lower()
            snapshot_pending_work_items = (
                list(goal_manager.get("pending_work_items", []))
                if isinstance(goal_manager.get("pending_work_items"), list)
                else []
            )
            audit_state_path = session_agent_state_path(
                runtime_root,
                username=normalized,
                session_id=session_id,
                service_id=service_id,
            )
            audit_record = read_json_file(audit_state_path)
            audit_state = normalize_audit_state((audit_record or {}).get("audit_state", "all_clear"))
            pending_paths = _service_pending_paths(
                runtime_root,
                username=normalized,
                session_id=session_id,
                service_id=service_id,
            )
            has_persisted_service_pending = any(read_jsonl(pending_path) for pending_path in pending_paths)
            if status in _TERMINAL_SERVICE_STATES or snapshot_state in _TERMINAL_SERVICE_STATES:
                continue
            if (
                status not in {"running", "queued"}
                and snapshot_state not in {"running", "queued"}
                and not snapshot_pending_work_items
                and audit_state == "all_clear"
                and not has_persisted_service_pending
            ):
                continue
            if any(
                str(item.get("kind") or "").strip().lower() not in _STALE_GOAL_MANAGER_PENDING_KINDS
                for item in snapshot_pending_work_items
                if isinstance(item, dict)
            ):
                continue
            for pending_path in pending_paths:
                remove_file_if_exists(pending_path)

            updated_at = utc_ts()
            goal_manager["state"] = target_state
            goal_manager["audit_state"] = "all_clear"
            goal_manager["pending_work_items"] = []
            goal_manager["updated_at"] = updated_at
            service_state["status"] = target_state
            service_state["updated_at"] = updated_at
            service_state["goal_manager"] = goal_manager
            write_json_file(service_state_path, service_state)

            if isinstance(audit_record, dict):
                audit_record["audit_state"] = "all_clear"
                audit_record["updated_at"] = updated_at
                write_json_file(audit_state_path, audit_record)
            reconciled.append(service_id)
    return reconciled


def load_session_audit_summary(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    preferred_service_id: str | None = None,
    prefer_authoritative_goal_manager: bool = False,
    allow_reconcile: bool = True,
) -> dict[str, object]:
    """Return the strongest audit state across every service joined to a session."""
    normalized = normalize_username(username)
    if allow_reconcile:
        reconcile_stale_session_service_states(
            runtime_root,
            username=normalized,
            session_id=session_id,
            preferred_service_id=preferred_service_id,
        )
    normalized_preferred_service_id = str(preferred_service_id or "").strip()
    goal_manager_state = read_json_file(
        session_goal_manager_state_path(runtime_root, username=normalized, session_id=session_id)
    )
    goal_manager_audit_state = ""
    goal_manager_updated_at = ""
    goal_manager_progress_state = ""
    goal_manager_runtime_state = ""
    goal_manager_is_authoritative = False
    if isinstance(goal_manager_state, dict):
        goal_manager_audit_state = normalize_audit_state(goal_manager_state.get("audit_state", "all_clear"))
        goal_manager_updated_at = str(goal_manager_state.get("updated_at", "") or "")
        goal_manager_progress_state = str(goal_manager_state.get("progress_state") or "").strip().lower()
        goal_manager_runtime_state = str(goal_manager_state.get("state") or "").strip().lower()
        goal_manager_is_authoritative = bool(
            goal_manager_progress_state in {"in_progress", "complete"}
            or bool(str(goal_manager_state.get("service_id") or "").strip())
            or "audit_state" in goal_manager_state
            or bool(goal_manager_state.get("goal_satisfied", False))
            or bool(str(goal_manager_state.get("summary") or "").strip())
            or bool(goal_manager_state.get("pending_work_items"))
            or goal_manager_runtime_state in {"running", "queued", "failed", "waiting", "complete"}
        )
    if not allow_reconcile:
        services: list[dict[str, str]] = []
        preferred_updated_at = ""
        preferred_audit_state = "all_clear"
        if normalized_preferred_service_id:
            preferred_audit_path = session_agent_state_path(
                runtime_root,
                username=normalized,
                session_id=session_id,
                service_id=normalized_preferred_service_id,
            )
            preferred_record = read_json_file(preferred_audit_path)
            if isinstance(preferred_record, dict):
                preferred_audit_state = normalize_audit_state(preferred_record.get("audit_state", "all_clear"))
                preferred_updated_at = str(preferred_record.get("updated_at", "") or "")
                services.append(
                    {
                        "service_id": normalized_preferred_service_id,
                        "audit_state": preferred_audit_state,
                        "updated_at": preferred_updated_at,
                    }
                )
        strongest = preferred_audit_state
        strongest_updated_at = preferred_updated_at
        if goal_manager_is_authoritative:
            goal_manager_is_fresh_enough = (
                not preferred_updated_at
                or (bool(goal_manager_updated_at) and goal_manager_updated_at >= preferred_updated_at)
            )
            if goal_manager_is_fresh_enough:
                strongest = goal_manager_audit_state
                strongest_updated_at = goal_manager_updated_at or strongest_updated_at
            elif (
                prefer_authoritative_goal_manager
                and goal_manager_audit_state == "all_clear"
                and goal_manager_runtime_state == "idle"
                and goal_manager_progress_state == "in_progress"
                and AUDIT_STATE_RANK[preferred_audit_state] <= AUDIT_STATE_RANK[goal_manager_audit_state]
            ):
                strongest = goal_manager_audit_state
                strongest_updated_at = goal_manager_updated_at or strongest_updated_at
        return {"audit_state": strongest, "updated_at": strongest_updated_at, "services": services}
    services_dir = session_services_dir(runtime_root, username=normalized, session_id=session_id)
    strongest = "all_clear"
    strongest_updated_at = ""
    strongest_service = "all_clear"
    strongest_service_updated_at = ""
    strongest_nonpreferred_service = "all_clear"
    strongest_noncanonical_service = "all_clear"
    services: list[dict[str, str]] = []
    with state_read_lock(runtime_root):
        for audit_path in sorted(services_dir.glob("*.audit.json")):
            record = read_json_file(audit_path)
            if not isinstance(record, dict):
                continue
            service_id = str(record.get("service_id") or audit_path.name.removesuffix(".audit.json")).strip()
            audit_state = normalize_audit_state(record.get("audit_state", "all_clear"))
            services.append(
                {
                    "service_id": service_id,
                    "audit_state": audit_state,
                    "updated_at": str(record.get("updated_at", "") or ""),
                }
            )
            record_updated_at = str(record.get("updated_at", "") or "")
            if AUDIT_STATE_RANK[audit_state] > AUDIT_STATE_RANK[strongest_service]:
                strongest_service = audit_state
                strongest_service_updated_at = record_updated_at
            elif audit_state == strongest_service and record_updated_at >= strongest_service_updated_at:
                strongest_service_updated_at = record_updated_at
            if service_id != normalized_preferred_service_id and AUDIT_STATE_RANK[audit_state] > AUDIT_STATE_RANK[
                strongest_nonpreferred_service
            ]:
                strongest_nonpreferred_service = audit_state
            if not CANONICAL_LLM_SERVICE_RE.match(service_id) and AUDIT_STATE_RANK[audit_state] > AUDIT_STATE_RANK[
                strongest_noncanonical_service
            ]:
                strongest_noncanonical_service = audit_state

        strongest = strongest_service
        strongest_updated_at = strongest_service_updated_at
        if goal_manager_is_authoritative:
            goal_manager_is_fresh_enough = (
                not strongest_service_updated_at
                or (
                    bool(goal_manager_updated_at)
                    and goal_manager_updated_at >= strongest_service_updated_at
                )
            )
            if goal_manager_is_fresh_enough:
                strongest = goal_manager_audit_state
                strongest_updated_at = goal_manager_updated_at or strongest_updated_at
            elif (
                prefer_authoritative_goal_manager
                and goal_manager_audit_state == "all_clear"
                and goal_manager_runtime_state == "idle"
                and goal_manager_progress_state == "in_progress"
                and (
                    AUDIT_STATE_RANK[strongest_nonpreferred_service]
                    <= AUDIT_STATE_RANK[goal_manager_audit_state]
                    or AUDIT_STATE_RANK[strongest_noncanonical_service]
                    <= AUDIT_STATE_RANK[goal_manager_audit_state]
                )
            ):
                strongest = goal_manager_audit_state
                strongest_updated_at = goal_manager_updated_at or strongest_updated_at

        return {"audit_state": strongest, "updated_at": strongest_updated_at, "services": services}


def save_agent_audit_state(
    runtime_root: Path,
    *,
    service_id: str,
    username: str,
    session_id: str,
    audit_state: str,
) -> None:
    """Persist the audit state for an agent-talk binding."""
    normalized = normalize_audit_state(audit_state)
    with state_lock(runtime_root):
        record = {
            "service_id": service_id,
            "username": normalize_username(username),
            "session_id": session_id,
            "audit_state": normalized,
            "updated_at": utc_ts(),
        }
        write_json_file(
            session_agent_state_path(
                runtime_root,
                username=username,
                session_id=session_id,
                service_id=service_id,
            ),
            record,
        )


def reset_agent_audit_states_for_session(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
) -> int:
    """Reset audit_state to ``all_clear`` for every agent bound to this session."""
    normalized = normalize_username(username)
    services_dir = session_services_dir(runtime_root, username=normalized, session_id=session_id)
    if not services_dir.exists():
        return 0

    cleared = 0
    with state_lock(runtime_root):
        for audit_path in sorted(services_dir.glob("*.audit.json")):
            record = read_json_file(audit_path)
            if not isinstance(record, dict):
                continue
            record["audit_state"] = "all_clear"
            record["updated_at"] = utc_ts()
            write_json_file(audit_path, dict(record))
            cleared += 1
        return cleared
