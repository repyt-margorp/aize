from __future__ import annotations

import os
import secrets
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from wire.protocol import utc_ts

from ._core import (
    DEFAULT_AUTO_COMPACT_THRESHOLD_LEFT_PERCENT,
    DEFAULT_AUTO_RESUME_INTERVAL_SECONDS,
    DEFAULT_SESSION_UI_MODE,
    DEFAULT_USER_RESPONSE_WAIT_TIMEOUT_SECONDS,
    DEFAULT_SESSION_GROUP,
    NATIVE_PROVIDER_KINDS,
    SESSION_GROUP_DEFAULT_PERMISSIONS,
    SESSION_UI_MODES,
    _active_goal_revision_unlocked,
    _apply_active_goal_snapshot_unlocked,
    _auth_sessions,
    _conversation_sessions,
    _ensure_default_session_unlocked,
    _ensure_session_defaults_unlocked,
    _ensure_session_exists_unlocked,
    _load_state_unlocked,
    write_state,
    digest_token,
    normalize_agent_priority,
    normalize_goal_manager_priority,
    normalize_child_session_sharing_policy,
    normalize_session_skills,
    ensure_session_storage_unlocked,
    normalize_auto_compact_threshold_left_percent,
    normalize_username,
    read_json_file,
    read_jsonl,
    sessions_dir,
    session_agent_state_path,
    session_dag_children_path,
    session_dag_parents_path,
    session_dir,
    session_agent_inbox_dir,
    session_agent_outbox_dir,
    session_agent_acl_path,
    session_metadata_path,
    session_goal_manager_state_path,
    session_service_state_path,
    session_timeline_path,
    session_user_dir,
    state_lock,
    state_read_lock,
    write_goal_dir,
    write_json_file,
)


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


def _utc_ts_after_seconds(seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=max(0, int(seconds)))).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _user_response_wait_is_due(session: dict[str, Any], *, now: datetime | None = None) -> bool:
    record = dict(session or {})
    if not bool(record.get("user_response_wait_active", False)):
        return False
    due_at = _parse_utc_ts(record.get("user_response_wait_until_at"))
    if due_at is None:
        return False
    return due_at <= (now or datetime.now(UTC))


def _clear_due_user_response_wait_unlocked(talk: dict[str, Any], *, now: datetime | None = None) -> bool:
    if not _user_response_wait_is_due(talk, now=now):
        return False
    talk["user_response_wait_active"] = False
    cleared_at = utc_ts()
    talk["user_response_wait_last_cleared_at"] = cleared_at
    talk["user_response_wait_last_timeout_at"] = cleared_at
    active_request_id = str(talk.get("user_response_wait_request_id") or "").strip()
    requests = talk.get("user_response_wait_requests")
    if isinstance(requests, list) and active_request_id:
        for item in reversed(requests):
            if not isinstance(item, dict):
                continue
            if str(item.get("request_id") or "").strip() != active_request_id:
                continue
            item["status"] = "timed_out"
            item["cleared_at"] = cleared_at
            break
    talk["updated_at"] = utc_ts()
    return True


def session_group_permissions(session: dict[str, Any] | None) -> dict[str, bool]:
    record = dict(session or {})
    _ensure_session_defaults_unlocked(record)
    permissions = record.get("session_permissions")
    if isinstance(permissions, dict):
        return {str(key): bool(value) for key, value in permissions.items()}
    group = str(record.get("session_group") or DEFAULT_SESSION_GROUP).strip().lower()
    return {
        str(key): bool(value)
        for key, value in SESSION_GROUP_DEFAULT_PERMISSIONS.get(
            group,
            SESSION_GROUP_DEFAULT_PERMISSIONS[DEFAULT_SESSION_GROUP],
        ).items()
    }


def session_ui_mode(session: dict[str, Any] | None) -> str:
    record = dict(session or {})
    _ensure_session_defaults_unlocked(record)
    requested = str(record.get("session_ui_mode") or "").strip().lower()
    if requested in SESSION_UI_MODES:
        return requested
    permissions = session_group_permissions(record)
    if str(record.get("session_group") or DEFAULT_SESSION_GROUP).strip().lower() == "root":
        return "map_only"
    if not bool(permissions.get("update_goal", True)) and not bool(permissions.get("send_prompt", True)):
        return "map_only"
    return DEFAULT_SESSION_UI_MODE


def session_uses_map_only_ui(session: dict[str, Any] | None) -> bool:
    return session_ui_mode(session) == "map_only"


def session_operation_allowed(session: dict[str, Any] | None, operation: str) -> bool:
    operation_name = str(operation or "").strip()
    if not operation_name:
        return False
    permissions = session_group_permissions(session)
    aliases = {
        "create_child_session": ("create_child_session", "create_session"),
        "create_session": ("create_session", "create_child_session"),
        "update_goal": ("update_goal", "update_session_goal"),
        "update_session_goal": ("update_session_goal", "update_goal"),
        "send_prompt": ("send_prompt", "send_user_prompt"),
        "send_user_prompt": ("send_user_prompt", "send_prompt"),
    }
    for key in aliases.get(operation_name, (operation_name,)):
        if key in permissions:
            return bool(permissions.get(key))
    return False


def session_child_sharing_policy(session: dict[str, Any] | None) -> dict[str, Any]:
    record = dict(session or {})
    _ensure_session_defaults_unlocked(record)
    return normalize_child_session_sharing_policy(record.get("child_session_sharing"))


def session_allows_child_session_creator(
    parent_session: dict[str, Any] | None,
    *,
    requester_session_id: str | None = None,
    requester_unit_id: str | None = None,
    requester_template_id: str | None = None,
) -> bool:
    parent = dict(parent_session or {})
    _ensure_session_defaults_unlocked(parent)
    if not session_operation_allowed(parent, "create_child_session"):
        return False
    parent_session_id = str(parent.get("session_id") or "").strip()
    requester_session = str(requester_session_id or "").strip()
    if not requester_session or requester_session == parent_session_id:
        return True
    policy = session_child_sharing_policy(parent)
    mode = str(policy.get("mode") or "private").strip().lower()
    if mode == "public":
        return True
    if mode != "allowlist":
        return False
    allowed_session_ids = {
        str(item).strip()
        for item in policy.get("allowed_source_session_ids", [])
        if str(item).strip()
    }
    if requester_session and requester_session in allowed_session_ids:
        return True
    requester_unit = str(requester_unit_id or requester_template_id or "").strip()
    allowed_unit_ids = {
        str(item).strip()
        for item in policy.get("allowed_source_unit_ids", policy.get("allowed_source_template_ids", []))
        if str(item).strip()
    }
    return bool(requester_unit and requester_unit in allowed_unit_ids)


def _list_session_records(runtime_root: Path, *, username: str) -> list[dict[str, Any]]:
    user_dir = session_user_dir(runtime_root, username=username)
    if not user_dir.exists():
        return []
    sessions: list[dict[str, Any]] = []
    for talk_dir in sorted(path for path in user_dir.iterdir() if path.is_dir()):
        stored = read_json_file(session_metadata_path(runtime_root, username=username, session_id=talk_dir.name))
        if not isinstance(stored, dict):
            continue
        _ensure_session_defaults_unlocked(stored)
        if _user_response_wait_is_due(stored):
            expired = consume_session_due_user_response_wait(
                runtime_root,
                username=username,
                session_id=talk_dir.name,
            )
            if isinstance(expired, dict):
                stored = expired
        sessions.append(dict(stored))
    return sessions


def list_sessions(runtime_root: Path, *, username: str) -> list[dict[str, Any]]:
    normalized = normalize_username(username)
    with state_lock(runtime_root):
        state = _load_state_unlocked(runtime_root)
        _ensure_default_session_unlocked(state, normalized)
    return _list_session_records(runtime_root, username=normalized)


def list_sessions_with_histories(
    runtime_root: Path, *, username: str
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    normalized = normalize_username(username)
    with state_lock(runtime_root):
        state = _load_state_unlocked(runtime_root)
        _ensure_default_session_unlocked(state, normalized)
    sessions = _list_session_records(runtime_root, username=normalized)
    histories: dict[str, list[dict[str, Any]]] = {}
    for session in sessions:
        session_id = str(session.get("session_id") or "")
        timeline_path = session_timeline_path(runtime_root, username=normalized, session_id=session_id)
        histories[session_id] = read_jsonl(timeline_path)
    return sessions, histories


def list_all_sessions_with_users(runtime_root: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    sessions_root = sessions_dir(runtime_root)
    if not sessions_root.exists():
        return result
    for user_dir in sorted(path for path in sessions_root.iterdir() if path.is_dir()):
        username = normalize_username(user_dir.name)
        for session in _list_session_records(runtime_root, username=username):
            entry = dict(session)
            entry["username"] = username
            result.append(entry)
    return result


# ---------------------------------------------------------------------------
# Per-agent file operations (inbox / outbox)
# ---------------------------------------------------------------------------

def _resolve_agent_box_dir(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    agent_id: str,
    box: str,
) -> Path | None:
    normalized_box = str(box or "").strip().lower()
    if normalized_box == "inbox":
        return session_agent_inbox_dir(runtime_root, username=username, session_id=session_id, agent_id=agent_id)
    if normalized_box == "outbox":
        return session_agent_outbox_dir(runtime_root, username=username, session_id=session_id, agent_id=agent_id)
    return None


def write_agent_file(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    agent_id: str,
    box: str,
    filename: str,
    content: bytes,
) -> bool:
    """Write content to an agent's inbox or outbox.  Returns True on success."""
    dir_path = _resolve_agent_box_dir(runtime_root, username=username, session_id=session_id, agent_id=agent_id, box=box)
    if dir_path is None:
        return False
    safe_filename = Path(filename).name
    if not safe_filename:
        return False
    dir_path.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=safe_filename + ".", suffix=".tmp", dir=dir_path)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, dir_path / safe_filename)
        return True
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def read_agent_file(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    agent_id: str,
    box: str,
    filename: str,
) -> bytes | None:
    """Read a file from an agent's inbox or outbox.  Returns None if not found."""
    dir_path = _resolve_agent_box_dir(runtime_root, username=username, session_id=session_id, agent_id=agent_id, box=box)
    if dir_path is None:
        return None
    safe_filename = Path(filename).name
    if not safe_filename:
        return None
    file_path = dir_path / safe_filename
    try:
        return file_path.read_bytes()
    except FileNotFoundError:
        return None


def list_agent_files(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    agent_id: str,
    box: str,
) -> list[dict[str, Any]]:
    """List files in an agent's inbox or outbox."""
    dir_path = _resolve_agent_box_dir(runtime_root, username=username, session_id=session_id, agent_id=agent_id, box=box)
    if dir_path is None or not dir_path.exists():
        return []
    files = []
    for entry in sorted(dir_path.iterdir()):
        if entry.is_file():
            stat = entry.stat()
            files.append({
                "filename": entry.name,
                "size": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            })
    return files


def delete_agent_file(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    agent_id: str,
    box: str,
    filename: str,
) -> bool:
    """Delete a file from an agent's inbox or outbox.  Returns True if deleted."""
    dir_path = _resolve_agent_box_dir(runtime_root, username=username, session_id=session_id, agent_id=agent_id, box=box)
    if dir_path is None:
        return False
    safe_filename = Path(filename).name
    if not safe_filename:
        return False
    try:
        (dir_path / safe_filename).unlink()
        return True
    except FileNotFoundError:
        return False


# ---------------------------------------------------------------------------
# Per-agent file directory ACL
# ---------------------------------------------------------------------------
# ACL JSON structure stored in `agent_files/{safe_agent_id}/.acl.json`:
# {
#   "owner": "<agent_id>",          # owner has implicit read+write
#   "grants": [                      # additional explicit grants
#     {"agent_id": "<id>", "permissions": ["read", "write"]}
#   ],
#   "updated_at": "<iso8601>"
# }
#
# Default when no .acl.json exists: only the owning agent (whose agent_id
# matches the directory) may access it.
#
# HTTP session-level callers (no caller_agent_id) are treated as session admin
# and bypass ACL.  Superuser sessions bypass ACL unconditionally.
# ---------------------------------------------------------------------------

def get_agent_file_dir_acl(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    agent_id: str,
) -> dict[str, Any]:
    """Return the ACL for an agent file directory.

    If no .acl.json exists, returns the implicit default:
    owner = agent_id, grants = [].
    """
    acl_path = session_agent_acl_path(runtime_root, username=username, session_id=session_id, agent_id=agent_id)
    try:
        raw = read_json_file(acl_path)
        if isinstance(raw, dict):
            return raw
    except (FileNotFoundError, OSError, ValueError):
        pass
    return {
        "owner": agent_id,
        "grants": [],
        "updated_at": "",
    }


def set_agent_file_dir_acl(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    agent_id: str,
    owner: str | None = None,
    grants: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Set the ACL for an agent file directory.

    ``grants`` is a list of ``{"agent_id": "...", "permissions": ["read", "write"]}``.
    Omitted fields keep their current value.
    Returns the updated ACL.
    """
    acl_path = session_agent_acl_path(runtime_root, username=username, session_id=session_id, agent_id=agent_id)
    current = get_agent_file_dir_acl(runtime_root, username=username, session_id=session_id, agent_id=agent_id)
    updated: dict[str, Any] = dict(current)
    if owner is not None:
        updated["owner"] = str(owner).strip()
    if grants is not None:
        # Validate and normalise grants
        normalised: list[dict[str, Any]] = []
        for g in grants:
            if not isinstance(g, dict):
                continue
            gid = str(g.get("agent_id") or "").strip()
            if not gid:
                continue
            perms = [str(p).strip().lower() for p in (g.get("permissions") or []) if str(p).strip().lower() in {"read", "write"}]
            normalised.append({"agent_id": gid, "permissions": perms})
        updated["grants"] = normalised
    updated["updated_at"] = utc_ts()
    acl_path.parent.mkdir(parents=True, exist_ok=True)
    write_json_file(acl_path, updated)
    return updated


def check_agent_file_acl(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    dir_agent_id: str,
    caller_agent_id: str,
    permission: str,
) -> bool:
    """Return True if caller_agent_id is allowed ``permission`` (``"read"`` or ``"write"``)
    on the file directory owned by ``dir_agent_id``.

    Caller is allowed when:
    - ``caller_agent_id == dir_agent_id`` (owner of the directory), OR
    - An entry in ``grants`` lists ``caller_agent_id`` with the required permission.
    """
    acl = get_agent_file_dir_acl(runtime_root, username=username, session_id=session_id, agent_id=dir_agent_id)
    owner = str(acl.get("owner") or dir_agent_id).strip()
    if caller_agent_id == owner:
        return True
    grants = acl.get("grants")
    if not isinstance(grants, list):
        return False
    perm = str(permission).strip().lower()
    for g in grants:
        if not isinstance(g, dict):
            continue
        if str(g.get("agent_id") or "").strip() == caller_agent_id:
            perms = [str(p).lower() for p in (g.get("permissions") or [])]
            if perm in perms:
                return True
    return False


def list_sessions_bound_to_service(runtime_root: Path, *, service_id: str) -> list[dict[str, Any]]:
    bound: list[dict[str, Any]] = []
    with state_lock(runtime_root):
        sessions_root = sessions_dir(runtime_root)
        if not sessions_root.exists():
            return bound
        for user_dir in sorted(path for path in sessions_root.iterdir() if path.is_dir()):
            username = normalize_username(user_dir.name)
            for session in _list_session_records(runtime_root, username=username):
                if str(session.get("service_id") or "") != service_id:
                    continue
                bound.append(
                    {
                        "username": username,
                        "session_id": str(session.get("session_id") or ""),
                        "service_id": service_id,
                    }
                )
    return bound


def create_conversation_session(
    runtime_root: Path,
    *,
    username: str,
    label: str | None = None,
    session_group: str | None = None,
    session_permissions: dict[str, Any] | None = None,
    created_by_username: str | None = None,
    created_by_type: str | None = None,
    parent_session_id: str | None = None,
    origin_session_id: str | None = None,
    origin_goal_id: str | None = None,
    origin_goal_text: str | None = None,
    session_ui_mode: str | None = None,
    session_interactive: bool = False,
    communication_agent_enabled: bool = False,
    communication_agent_priority: list[str] | None = None,
    child_session_sharing: dict[str, Any] | None = None,
    session_skills: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    normalized = normalize_username(username)
    normalized_parent_session_id = str(parent_session_id or "").strip()
    created_session_id = ""
    with state_lock(runtime_root):
        state = _load_state_unlocked(runtime_root)
        sessions = _conversation_sessions(state).setdefault(normalized, [])
        normalized_group = str(session_group or DEFAULT_SESSION_GROUP).strip().lower() or DEFAULT_SESSION_GROUP
        if normalized_group not in SESSION_GROUP_DEFAULT_PERMISSIONS:
            normalized_group = DEFAULT_SESSION_GROUP
        defaults = SESSION_GROUP_DEFAULT_PERMISSIONS.get(
            normalized_group,
            SESSION_GROUP_DEFAULT_PERMISSIONS[DEFAULT_SESSION_GROUP],
        )
        normalized_permissions: dict[str, bool] = {}
        if isinstance(session_permissions, dict):
            for operation_name, default_value in defaults.items():
                if operation_name in session_permissions:
                    normalized_permissions[operation_name] = bool(session_permissions.get(operation_name))
                else:
                    normalized_permissions[operation_name] = bool(default_value)
        else:
            normalized_permissions = {str(key): bool(value) for key, value in defaults.items()}
        session = {
            "session_id": secrets.token_hex(8),
            "label": (label or "").strip() or f"Session {len(sessions) + 1}",
            "session_group": normalized_group,
            "session_ui_mode": str(session_ui_mode or "").strip().lower(),
            "session_interactive": bool(session_interactive),
            "communication_agent_enabled": bool(communication_agent_enabled),
            "communication_agent_priority": [
                dict(item) if isinstance(item, dict) else str(item).strip()
                for item in (communication_agent_priority or [])
                if isinstance(item, dict) or str(item).strip()
            ],
            "session_permissions": normalized_permissions,
            "child_session_sharing": normalize_child_session_sharing_policy(child_session_sharing),
            "session_skills": normalize_session_skills(session_skills),
            "auto_resume_enabled": False,
            "auto_compact_threshold_left_percent": DEFAULT_AUTO_COMPACT_THRESHOLD_LEFT_PERCENT,
            "created_at": utc_ts(),
            "updated_at": utc_ts(),
            "created_by_username": str(created_by_username or normalized).strip(),
            "created_by_type": str(created_by_type or "user").strip().lower() or "user",
            "parent_session_id": normalized_parent_session_id,
            "origin_session_id": str(origin_session_id or normalized_parent_session_id).strip(),
            "origin_goal_id": str(origin_goal_id or "").strip(),
            "origin_goal_text": str(origin_goal_text or ""),
        }
        sessions.append(session)
        created_session_id = str(session["session_id"])
        ensure_session_storage_unlocked(runtime_root, username=normalized, session=session)
        write_state(runtime_root, state)
    if normalized_parent_session_id and created_session_id:
        add_session_child(
            runtime_root,
            username=normalized,
            parent_session_id=normalized_parent_session_id,
            child_session_id=created_session_id,
        )
        linked_session = get_session_settings(runtime_root, username=normalized, session_id=created_session_id)
        if isinstance(linked_session, dict):
            return linked_session
    return dict(session)


def get_session_service(runtime_root: Path, *, username: str, session_id: str) -> str | None:
    normalized = normalize_username(username)
    with state_lock(runtime_root):
        session = read_json_file(session_metadata_path(runtime_root, username=normalized, session_id=session_id))
        if isinstance(session, dict):
            service_id = session.get("service_id")
            return str(service_id) if isinstance(service_id, str) and service_id else None
        return None


def _clear_superseded_service_session_state(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    service_id: str,
) -> None:
    normalized_service_id = str(service_id or "").strip()
    if not normalized_service_id:
        return
    audit_path = session_agent_state_path(
        runtime_root,
        username=username,
        session_id=session_id,
        service_id=normalized_service_id,
    )
    audit_record = read_json_file(audit_path)
    if isinstance(audit_record, dict):
        audit_record["audit_state"] = "all_clear"
        audit_record["updated_at"] = utc_ts()
        write_json_file(audit_path, audit_record)

    service_state_path = session_service_state_path(
        runtime_root,
        username=username,
        session_id=session_id,
        service_id=normalized_service_id,
    )
    service_state = read_json_file(service_state_path)
    if isinstance(service_state, dict):
        service_state["status"] = "idle"
        service_state["updated_at"] = utc_ts()
        goal_manager = service_state.get("goal_manager")
        if isinstance(goal_manager, dict):
            goal_manager["state"] = "idle"
            goal_manager["audit_state"] = "all_clear"
            goal_manager["updated_at"] = service_state["updated_at"]
            service_state["goal_manager"] = goal_manager
        write_json_file(service_state_path, service_state)


def lease_session_service(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    pool_service_ids: list[str],
) -> str | None:
    normalized = normalize_username(username)
    pool = [service_id for service_id in pool_service_ids if isinstance(service_id, str) and service_id]
    if not pool:
        return None
    with state_lock(runtime_root):
        session_path = session_metadata_path(runtime_root, username=normalized, session_id=session_id)
        target_session = read_json_file(session_path)
        if not isinstance(target_session, dict):
            return None
        _ensure_session_defaults_unlocked(target_session)
        existing_service_id = target_session.get("service_id")
        if isinstance(existing_service_id, str) and existing_service_id in pool:
            return existing_service_id
        # Collect all sessions currently holding a pool slot, keyed by service_id.
        # Value is (holder_username, holder_session_id, holder_session_priority).
        leased: dict[str, tuple[str, str, int]] = {}
        sessions_root = sessions_dir(runtime_root)
        if sessions_root.exists():
            for user_dir in sorted(path for path in sessions_root.iterdir() if path.is_dir()):
                for talk_dir in sorted(path for path in user_dir.iterdir() if path.is_dir()):
                    talk = read_json_file(session_metadata_path(runtime_root, username=user_dir.name, session_id=talk_dir.name))
                    if not isinstance(talk, dict):
                        continue
                    svc_id = talk.get("service_id")
                    if isinstance(svc_id, str) and svc_id in pool:
                        raw_prio = talk.get("session_priority", 50)
                        prio = max(0, min(100, int(raw_prio))) if isinstance(raw_prio, (int, float)) else 50
                        leased[svc_id] = (user_dir.name, talk_dir.name, prio)
        available = next((service_id for service_id in pool if service_id not in leased), None)
        if available is not None:
            previous_service_id = str(existing_service_id or "").strip()
            if previous_service_id and previous_service_id != available:
                _clear_superseded_service_session_state(
                    runtime_root,
                    username=normalized,
                    session_id=session_id,
                    service_id=previous_service_id,
                )
            target_session["service_id"] = available
            target_session["updated_at"] = utc_ts()
            ensure_session_storage_unlocked(runtime_root, username=normalized, session=target_session)
            return available

        # No free slot — attempt priority-based preemption.
        # If the current session has higher session_priority than the lowest-priority holder,
        # revoke that holder's lease and grant it to the current session.
        raw_my_prio = target_session.get("session_priority", 50)
        my_priority = max(0, min(100, int(raw_my_prio))) if isinstance(raw_my_prio, (int, float)) else 50
        if leased:
            victim_svc, (victim_user, victim_sid, victim_prio) = min(leased.items(), key=lambda kv: kv[1][2])
            if my_priority > victim_prio:
                victim_path = session_metadata_path(runtime_root, username=victim_user, session_id=victim_sid)
                victim_session = read_json_file(victim_path)
                if isinstance(victim_session, dict):
                    victim_session.pop("service_id", None)
                    victim_session["updated_at"] = utc_ts()
                    ensure_session_storage_unlocked(runtime_root, username=victim_user, session=victim_session)
                previous_service_id = str(existing_service_id or "").strip()
                if previous_service_id and previous_service_id != victim_svc:
                    _clear_superseded_service_session_state(
                        runtime_root,
                        username=normalized,
                        session_id=session_id,
                        service_id=previous_service_id,
                    )
                target_session["service_id"] = victim_svc
                target_session["updated_at"] = utc_ts()
                ensure_session_storage_unlocked(runtime_root, username=normalized, session=target_session)
                return victim_svc

        return None


def release_session_service(runtime_root: Path, *, username: str, session_id: str) -> str | None:
    normalized = normalize_username(username)
    with state_lock(runtime_root):
        session_path = session_metadata_path(runtime_root, username=normalized, session_id=session_id)
        session = read_json_file(session_path)
        if not isinstance(session, dict):
            return None
        _ensure_session_defaults_unlocked(session)
        service_id = session.get("service_id")
        if not isinstance(service_id, str) or not service_id:
            return None
        session.pop("service_id", None)
        session["updated_at"] = utc_ts()
        ensure_session_storage_unlocked(runtime_root, username=normalized, session=session)
        return service_id


def release_nonrunnable_session_services(runtime_root: Path) -> list[dict[str, str]]:
    from kernel.lifecycle import load_lifecycle_state
    from kernel.registry import load_registry

    services = load_registry(runtime_root).get("services", {})
    processes = load_lifecycle_state(runtime_root).get("processes", {})
    terminal_statuses = {"stopped", "failed", "crashed", "dead", "exited"}

    def release_reason_for_service(service_id: str) -> str | None:
        service = services.get(service_id)
        if not isinstance(service, dict):
            return "service_missing"
        service_status = str(service.get("status") or "").strip().lower()
        if service_status in terminal_statuses:
            return f"service_status:{service_status}"
        current_process_id = service.get("current_process_id")
        if not isinstance(current_process_id, str) or not current_process_id:
            return None
        process = processes.get(current_process_id)
        if not isinstance(process, dict):
            return "process_missing"
        process_status = str(process.get("status") or "").strip().lower()
        if process_status in terminal_statuses:
            return f"process_status:{process_status}"
        return None

    released: list[dict[str, str]] = []
    with state_lock(runtime_root):
        sessions_root = sessions_dir(runtime_root)
        if not sessions_root.exists():
            return released
        for user_dir in sorted(path for path in sessions_root.iterdir() if path.is_dir()):
            username = normalize_username(user_dir.name)
            for talk_dir in sorted(path for path in user_dir.iterdir() if path.is_dir()):
                session_path = session_metadata_path(runtime_root, username=username, session_id=talk_dir.name)
                talk = read_json_file(session_path)
                if not isinstance(talk, dict):
                    continue
                service_id = talk.get("service_id")
                if not isinstance(service_id, str) or not service_id:
                    continue
                _ensure_session_defaults_unlocked(talk)
                goal_active = bool(talk.get("goal_active", False))
                goal_completed = bool(talk.get("goal_completed", False))
                goal_progress_state = str(
                    talk.get("goal_progress_state", "complete" if goal_completed else "in_progress")
                ).strip().lower()
                release_reason = release_reason_for_service(service_id)
                if (
                    goal_completed
                    or not goal_active
                    or goal_progress_state == "complete"
                    or release_reason
                ):
                    released.append(
                        {
                            "username": username,
                            "session_id": str(talk.get("session_id", "")),
                            "service_id": service_id,
                            "reason": release_reason or "goal_inactive",
                        }
                    )
                    talk.pop("service_id", None)
                    talk["updated_at"] = utc_ts()
                    write_json_file(session_path, talk)
    return released


def reconcile_session_waiting_on_children(runtime_root: Path) -> list[dict[str, Any]]:
    reconciled: list[dict[str, Any]] = []
    with state_lock(runtime_root):
        sessions_root = sessions_dir(runtime_root)
        if not sessions_root.exists():
            return reconciled
        for user_dir in sorted(path for path in sessions_root.iterdir() if path.is_dir()):
            username = normalize_username(user_dir.name)
            for talk_dir in sorted(path for path in user_dir.iterdir() if path.is_dir()):
                session_path = session_metadata_path(
                    runtime_root,
                    username=username,
                    session_id=talk_dir.name,
                )
                talk = read_json_file(session_path)
                if not isinstance(talk, dict):
                    continue
                _ensure_session_defaults_unlocked(talk)
                session_id = str(talk.get("session_id") or talk_dir.name).strip()
                if not session_id:
                    continue
                completed_recovery_children = _complete_resumed_recovery_children_unlocked(
                    runtime_root,
                    username=username,
                    parent_session_id=session_id,
                )
                remaining_children = _list_active_in_progress_child_sessions_unlocked(
                    runtime_root,
                    username=username,
                    session_id=session_id,
                )
                expected_waiting = bool(remaining_children)
                if (
                    bool(talk.get("waiting_on_children", False)) == expected_waiting
                    and not completed_recovery_children
                ):
                    continue
                talk["waiting_on_children"] = expected_waiting
                talk["updated_at"] = utc_ts()
                ensure_session_storage_unlocked(runtime_root, username=username, session=talk)
                reconciled.append(
                    {
                        "username": username,
                        "session_id": session_id,
                        "waiting_on_children": expected_waiting,
                        "remaining_children": remaining_children,
                        "completed_recovery_children": completed_recovery_children,
                    }
                )
    return reconciled


def get_session_settings(runtime_root: Path, *, username: str, session_id: str) -> dict[str, Any] | None:
    normalized = normalize_username(username)
    with state_lock(runtime_root):
        stored = read_json_file(session_metadata_path(runtime_root, username=normalized, session_id=session_id))
        if isinstance(stored, dict):
            _ensure_session_defaults_unlocked(stored)
            if _clear_due_user_response_wait_unlocked(stored):
                ensure_session_storage_unlocked(runtime_root, username=normalized, session=stored)
                return dict(stored)
            ensure_session_storage_unlocked(runtime_root, username=normalized, session=stored)
            return dict(stored)
        return None


def claim_session_restart_resume(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    run_id: str,
    service_id: str,
) -> bool:
    normalized = normalize_username(username)
    normalized_run_id = str(run_id or "").strip()
    normalized_service_id = str(service_id or "").strip()
    if not normalized_run_id or not normalized_service_id:
        return False
    with state_lock(runtime_root):
        state = _load_state_unlocked(runtime_root)
        if not _ensure_session_exists_unlocked(state, normalized, session_id):
            return False
        for talk in _conversation_sessions(state).get(normalized, []):
            if not isinstance(talk, dict) or str(talk.get("session_id")) != session_id:
                continue
            _ensure_session_defaults_unlocked(talk)
            claimed_run_id = str(talk.get("restart_resume_claim_run_id") or "").strip()
            if claimed_run_id == normalized_run_id:
                return False
            talk["restart_resume_claim_run_id"] = normalized_run_id
            talk["restart_resume_claim_service_id"] = normalized_service_id
            talk["restart_resume_claimed_at"] = utc_ts()
            talk["updated_at"] = utc_ts()
            ensure_session_storage_unlocked(runtime_root, username=normalized, session=talk)
            return True
        return False


def update_session_auto_compact_threshold(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    threshold_left_percent: Any,
) -> dict[str, Any] | None:
    normalized = normalize_username(username)
    with state_lock(runtime_root):
        state = _load_state_unlocked(runtime_root)
        if not _ensure_session_exists_unlocked(state, normalized, session_id):
            return None
        normalized_threshold = normalize_auto_compact_threshold_left_percent(threshold_left_percent)
        for talk in _conversation_sessions(state).get(normalized, []):
            if isinstance(talk, dict) and str(talk.get("session_id")) == session_id:
                _ensure_session_defaults_unlocked(talk)
                talk["auto_compact_threshold_left_percent"] = normalized_threshold
                talk["updated_at"] = utc_ts()
                ensure_session_storage_unlocked(runtime_root, username=normalized, session=talk)
                return dict(talk)
        return None


def update_session_goal(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    goal_text: Any,
    updated_by_username: str | None = None,
    updated_by_type: str | None = None,
    origin_session_id: str | None = None,
    origin_goal_id: str | None = None,
    origin_goal_text: str | None = None,
) -> dict[str, Any] | None:
    normalized = normalize_username(username)
    with state_lock(runtime_root):
        state = _load_state_unlocked(runtime_root)
        if not _ensure_session_exists_unlocked(state, normalized, session_id):
            return None
        normalized_goal = str(goal_text or "").strip()
        for talk in _conversation_sessions(state).get(normalized, []):
            if isinstance(talk, dict) and str(talk.get("session_id")) == session_id:
                _ensure_session_defaults_unlocked(talk)
                previous_goal = _active_goal_revision_unlocked(talk)
                revision_ts = utc_ts()
                new_revision = {
                    "goal_id": secrets.token_hex(8),
                    "previous_goal_id": (
                        str(previous_goal.get("goal_id") or "").strip() or None
                    ) if isinstance(previous_goal, dict) else None,
                    "goal_text": normalized_goal,
                    "goal_active": bool(normalized_goal),
                    "goal_completed": not bool(normalized_goal),
                    "goal_progress_state": "in_progress" if normalized_goal else "complete",
                    "created_at": revision_ts,
                    "updated_at": revision_ts,
                    "updated_by_username": str(updated_by_username or normalized).strip(),
                    "updated_by_type": str(updated_by_type or "user").strip().lower() or "user",
                    "origin_session_id": str(origin_session_id or session_id).strip(),
                    "origin_goal_id": str(origin_goal_id or "").strip(),
                    "origin_goal_text": str(origin_goal_text or ""),
                }
                goal_history = talk.get("goal_history")
                if not isinstance(goal_history, list):
                    goal_history = []
                goal_history.append(new_revision)
                talk["goal_history"] = goal_history
                talk["active_goal_id"] = str(new_revision["goal_id"])
                _apply_active_goal_snapshot_unlocked(talk)
                talk["updated_at"] = utc_ts()
                ensure_session_storage_unlocked(runtime_root, username=normalized, session=talk)
                write_goal_dir(runtime_root, username=normalized, session_id=session_id, revision=new_revision)
                return dict(talk)
        return None


def update_session_goal_flags(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    goal_id: Any | None = None,
    goal_active: Any | None = None,
    goal_completed: Any | None = None,
    goal_progress_state: Any | None = None,
    goal_reset_completed_on_prompt: Any | None = None,
    goal_auto_compact_enabled: Any | None = None,
    agent_welcome_enabled: Any | None = None,
    preferred_provider: Any | None = None,
    auto_resume_enabled: Any | None = None,
    auto_resume_interval_seconds: Any | None = None,
    agent_priority: Any | None = None,
    goal_manager_priority: Any | None = None,
    session_priority: Any | None = None,
) -> dict[str, Any] | None:
    normalized = normalize_username(username)
    with state_lock(runtime_root):
        state = _load_state_unlocked(runtime_root)
        if not _ensure_session_exists_unlocked(state, normalized, session_id):
            return None
        for talk in _conversation_sessions(state).get(normalized, []):
            if isinstance(talk, dict) and str(talk.get("session_id")) == session_id:
                _ensure_session_defaults_unlocked(talk)
                target_goal_id = str(goal_id or talk.get("active_goal_id") or "").strip()
                target_revision: dict[str, Any] | None = None
                goal_history = talk.get("goal_history")
                if isinstance(goal_history, list):
                    for revision in goal_history:
                        if isinstance(revision, dict) and str(revision.get("goal_id") or "").strip() == target_goal_id:
                            target_revision = revision
                            break
                if target_revision is None:
                    target_revision = _active_goal_revision_unlocked(talk)

                goal_status_updates = any(
                    update is not None
                    for update in (
                        goal_active,
                        goal_completed,
                        goal_progress_state,
                    )
                )
                if target_revision is None and goal_status_updates:
                    if not isinstance(goal_history, list):
                        goal_history = []
                    revision_ts = utc_ts()
                    goal_text = str(talk.get("goal_text") or "").strip()
                    target_revision = {
                        "goal_id": secrets.token_hex(8),
                        "previous_goal_id": (
                            str(talk.get("goal_id") or talk.get("active_goal_id") or "").strip() or None
                        ),
                        "goal_text": goal_text,
                        "goal_active": bool(goal_active) if goal_active is not None else bool(goal_text),
                        "goal_completed": bool(goal_completed) if goal_completed is not None else bool(talk.get("goal_completed", False)),
                        "goal_progress_state": "in_progress",
                        "created_at": revision_ts,
                        "updated_at": revision_ts,
                    }
                    if goal_progress_state is not None:
                        normalized_progress_state = str(goal_progress_state).strip().lower()
                        target_revision["goal_progress_state"] = (
                            normalized_progress_state
                            if normalized_progress_state in {"complete", "in_progress"}
                            else "in_progress"
                        )
                        target_revision["goal_completed"] = (
                            target_revision["goal_progress_state"] == "complete"
                        )
                    goal_history.append(target_revision)
                    talk["goal_history"] = goal_history
                    talk["active_goal_id"] = str(target_revision["goal_id"])
                if target_revision is not None:
                    if goal_active is not None:
                        target_revision["goal_active"] = bool(goal_active)
                        if bool(goal_active):
                            talk["active_goal_id"] = str(target_revision.get("goal_id") or "")
                    if goal_completed is not None:
                        target_revision["goal_completed"] = bool(goal_completed)
                        target_revision["goal_progress_state"] = (
                            "complete" if bool(goal_completed) else "in_progress"
                        )
                    if goal_progress_state is not None:
                        progress_state = str(goal_progress_state).strip().lower()
                        target_revision["goal_progress_state"] = (
                            progress_state if progress_state in {"complete", "in_progress"} else "in_progress"
                        )
                        target_revision["goal_completed"] = (
                            target_revision["goal_progress_state"] == "complete"
                        )
                    if (
                        str(talk.get("goal_completion_policy") or "").strip().lower() == "continuous"
                        and str(target_revision.get("goal_text") or "").strip()
                    ):
                        target_revision["goal_active"] = True
                        target_revision["goal_completed"] = False
                        target_revision["goal_progress_state"] = "in_progress"
                    target_revision["updated_at"] = utc_ts()
                if goal_reset_completed_on_prompt is not None:
                    talk["goal_reset_completed_on_prompt"] = bool(goal_reset_completed_on_prompt)
                if goal_auto_compact_enabled is not None:
                    talk["goal_auto_compact_enabled"] = bool(goal_auto_compact_enabled)
                if agent_welcome_enabled is not None:
                    talk["agent_welcome_enabled"] = bool(agent_welcome_enabled)
                if preferred_provider is not None:
                    provider = str(preferred_provider).strip().lower()
                    talk["preferred_provider"] = provider if provider in set(NATIVE_PROVIDER_KINDS) else "codex"
                if agent_priority is not None:
                    talk["agent_priority"] = normalize_agent_priority(agent_priority)
                if goal_manager_priority is not None:
                    talk["goal_manager_priority"] = normalize_goal_manager_priority(goal_manager_priority)
                if session_priority is not None:
                    try:
                        talk["session_priority"] = max(0, min(100, int(session_priority)))
                    except (TypeError, ValueError):
                        pass
                if auto_resume_enabled is not None:
                    talk["auto_resume_enabled"] = bool(auto_resume_enabled)
                    if not bool(auto_resume_enabled):
                        talk["auto_resume_next_at"] = ""
                        talk["auto_resume_reason"] = ""
                if auto_resume_interval_seconds is not None:
                    try:
                        talk["auto_resume_interval_seconds"] = max(300, int(auto_resume_interval_seconds))
                    except (TypeError, ValueError):
                        talk["auto_resume_interval_seconds"] = DEFAULT_AUTO_RESUME_INTERVAL_SECONDS
                _apply_active_goal_snapshot_unlocked(talk)
                if goal_completed is not None:
                    if bool(goal_completed) and bool(talk.get("goal_active", False)) and bool(talk.get("auto_resume_enabled", False)):
                        talk["auto_resume_next_at"] = _utc_ts_after_seconds(
                            int(talk.get("auto_resume_interval_seconds", DEFAULT_AUTO_RESUME_INTERVAL_SECONDS))
                        )
                        talk["auto_resume_reason"] = "goal_completed_interval"
                        talk["auto_resume_last_scheduled_at"] = utc_ts()
                    elif not bool(goal_completed):
                        talk["auto_resume_next_at"] = ""
                        talk["auto_resume_reason"] = ""
                        talk["auto_resume_last_error"] = ""
                        talk["auto_resume_last_started_at"] = utc_ts()
                talk["updated_at"] = utc_ts()
                ensure_session_storage_unlocked(runtime_root, username=normalized, session=talk)
                if target_revision is not None:
                    write_goal_dir(runtime_root, username=normalized, session_id=session_id, revision=target_revision)
                return dict(talk)
        return None


def sync_communication_goal_progress(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    completed: bool,
    session: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    talk = dict(session or get_session_settings(runtime_root, username=username, session_id=session_id) or {})
    if not talk:
        return None
    if not str(talk.get("goal_text") or "").strip() or not bool(talk.get("goal_active", False)):
        return None
    if not (
        bool(talk.get("session_interactive", False))
        or bool(talk.get("communication_agent_enabled", False))
        or session_ui_mode(talk) == "communication"
    ):
        return None
    if completed and str(talk.get("goal_completion_policy") or "").strip().lower() == "continuous":
        return talk
    return update_session_goal_flags(
        runtime_root,
        username=username,
        session_id=session_id,
        goal_completed=bool(completed),
        goal_progress_state="complete" if completed else "in_progress",
    )


def update_session_user_response_wait(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    active: Any,
    timeout_seconds: Any | None = None,
    prompt_text: Any | None = None,
    request_id: Any | None = None,
    request_reason: Any | None = None,
    source_service_id: Any | None = None,
    requested_by_role: Any | None = None,
    response_request_ids: list[str] | None = None,
    cleared_reason: str | None = None,
) -> dict[str, Any] | None:
    normalized = normalize_username(username)
    with state_lock(runtime_root):
        state = _load_state_unlocked(runtime_root)
        if not _ensure_session_exists_unlocked(state, normalized, session_id):
            return None
        for talk in _conversation_sessions(state).get(normalized, []):
            if not isinstance(talk, dict) or str(talk.get("session_id")) != session_id:
                continue
            _ensure_session_defaults_unlocked(talk)
            wait_active = bool(active)
            if wait_active:
                try:
                    requested_timeout_seconds = int(
                        timeout_seconds
                        if timeout_seconds is not None
                        else talk.get(
                            "user_response_wait_timeout_seconds",
                            DEFAULT_USER_RESPONSE_WAIT_TIMEOUT_SECONDS,
                        )
                    )
                except (TypeError, ValueError):
                    requested_timeout_seconds = DEFAULT_USER_RESPONSE_WAIT_TIMEOUT_SECONDS
                effective_timeout_seconds = max(
                    60,
                    min(DEFAULT_USER_RESPONSE_WAIT_TIMEOUT_SECONDS, requested_timeout_seconds),
                )
                started_at = utc_ts()
                normalized_request_id = str(request_id or "").strip() or f"user-response-{uuid.uuid4().hex[:12]}"
                prompt = str(prompt_text or "").strip()
                reason = str(request_reason or "").strip()
                source = str(source_service_id or "").strip()
                role = str(requested_by_role or "").strip() or "goal_manager"
                talk["user_response_wait_active"] = True
                talk["user_response_wait_request_id"] = normalized_request_id
                talk["user_response_wait_timeout_seconds"] = requested_timeout_seconds
                talk["user_response_wait_effective_timeout_seconds"] = effective_timeout_seconds
                talk["user_response_wait_started_at"] = started_at
                talk["user_response_wait_generated_at"] = started_at
                talk["user_response_wait_until_at"] = _utc_ts_after_seconds(effective_timeout_seconds)
                talk["user_response_wait_prompt_text"] = prompt
                talk["user_response_wait_reason"] = reason
                talk["user_response_wait_source_service_id"] = source
                talk["user_response_wait_requested_by_role"] = role
                requests = talk.get("user_response_wait_requests")
                if not isinstance(requests, list):
                    requests = []
                requests.append(
                    {
                        "request_id": normalized_request_id,
                        "generated_at": started_at,
                        "started_at": started_at,
                        "until_at": talk["user_response_wait_until_at"],
                        "timeout_seconds": requested_timeout_seconds,
                        "effective_timeout_seconds": effective_timeout_seconds,
                        "question": prompt,
                        "reason": reason,
                        "source_service_id": source,
                        "requested_by_role": role,
                        "status": "waiting",
                    }
                )
                talk["user_response_wait_requests"] = requests[-50:]
            else:
                was_active = bool(talk.get("user_response_wait_active", False))
                active_request_id = str(talk.get("user_response_wait_request_id") or "").strip()
                answered_request_ids = {
                    str(value).strip()
                    for value in (response_request_ids or [])
                    if str(value).strip()
                }
                if not answered_request_ids and active_request_id:
                    answered_request_ids.add(active_request_id)
                talk["user_response_wait_active"] = False
                if was_active:
                    cleared_at = utc_ts()
                    talk["user_response_wait_last_cleared_at"] = cleared_at
                    status = "answered"
                    if str(cleared_reason or "").strip() == "timeout":
                        talk["user_response_wait_last_timeout_at"] = cleared_at
                        status = "timed_out"
                    requests = talk.get("user_response_wait_requests")
                    if isinstance(requests, list) and answered_request_ids:
                        for item in reversed(requests):
                            if not isinstance(item, dict):
                                continue
                            if str(item.get("request_id") or "").strip() not in answered_request_ids:
                                continue
                            item["status"] = status
                            item["cleared_at"] = cleared_at
                            if status == "answered":
                                item["answered_by_user"] = True
                elif answered_request_ids:
                    cleared_at = utc_ts()
                    requests = talk.get("user_response_wait_requests")
                    if isinstance(requests, list):
                        for item in requests:
                            if not isinstance(item, dict):
                                continue
                            if str(item.get("request_id") or "").strip() not in answered_request_ids:
                                continue
                            item["status"] = "answered"
                            item["cleared_at"] = cleared_at
                            item["answered_by_user"] = True
            talk["updated_at"] = utc_ts()
            ensure_session_storage_unlocked(runtime_root, username=normalized, session=talk)
            return dict(talk)
        return None


def record_session_user_response_request(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    status: Any,
    timeout_seconds: Any | None = None,
    prompt_text: Any | None = None,
    request_id: Any | None = None,
    request_reason: Any | None = None,
    source_service_id: Any | None = None,
    requested_by_role: Any | None = None,
) -> dict[str, Any] | None:
    normalized = normalize_username(username)
    with state_lock(runtime_root):
        state = _load_state_unlocked(runtime_root)
        if not _ensure_session_exists_unlocked(state, normalized, session_id):
            return None
        for talk in _conversation_sessions(state).get(normalized, []):
            if not isinstance(talk, dict) or str(talk.get("session_id")) != session_id:
                continue
            _ensure_session_defaults_unlocked(talk)
            try:
                requested_timeout_seconds = int(timeout_seconds or DEFAULT_USER_RESPONSE_WAIT_TIMEOUT_SECONDS)
            except (TypeError, ValueError):
                requested_timeout_seconds = DEFAULT_USER_RESPONSE_WAIT_TIMEOUT_SECONDS
            effective_timeout_seconds = max(
                60,
                min(DEFAULT_USER_RESPONSE_WAIT_TIMEOUT_SECONDS, requested_timeout_seconds),
            )
            recorded_at = utc_ts()
            normalized_request_id = str(request_id or "").strip() or f"user-response-{uuid.uuid4().hex[:12]}"
            prompt = str(prompt_text or "").strip()
            reason = str(request_reason or "").strip()
            source = str(source_service_id or "").strip()
            role = str(requested_by_role or "").strip() or "agent"
            normalized_status = str(status or "recorded").strip() or "recorded"
            talk["user_response_wait_request_id"] = normalized_request_id
            talk["user_response_wait_timeout_seconds"] = requested_timeout_seconds
            talk["user_response_wait_effective_timeout_seconds"] = effective_timeout_seconds
            talk["user_response_wait_generated_at"] = recorded_at
            talk["user_response_wait_started_at"] = ""
            talk["user_response_wait_until_at"] = _utc_ts_after_seconds(effective_timeout_seconds)
            talk["user_response_wait_prompt_text"] = prompt
            talk["user_response_wait_reason"] = reason
            talk["user_response_wait_source_service_id"] = source
            talk["user_response_wait_requested_by_role"] = role
            talk["user_response_wait_active"] = False
            talk.pop("user_response_wait_last_timeout_at", None)
            requests = talk.get("user_response_wait_requests")
            if not isinstance(requests, list):
                requests = []
            requests.append(
                {
                    "request_id": normalized_request_id,
                    "generated_at": recorded_at,
                    "started_at": recorded_at,
                    "until_at": _utc_ts_after_seconds(effective_timeout_seconds),
                    "timeout_seconds": requested_timeout_seconds,
                    "effective_timeout_seconds": effective_timeout_seconds,
                    "question": prompt,
                    "reason": reason,
                    "source_service_id": source,
                    "requested_by_role": role,
                    "status": normalized_status,
                }
            )
            talk["user_response_wait_requests"] = requests[-50:]
            talk["updated_at"] = utc_ts()
            ensure_session_storage_unlocked(runtime_root, username=normalized, session=talk)
            return dict(talk)
        return None


def consume_session_due_user_response_wait(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
) -> dict[str, Any] | None:
    normalized = normalize_username(username)
    with state_lock(runtime_root):
        state = _load_state_unlocked(runtime_root)
        if not _ensure_session_exists_unlocked(state, normalized, session_id):
            return None
        now = datetime.now(UTC)
        for talk in _conversation_sessions(state).get(normalized, []):
            if not isinstance(talk, dict) or str(talk.get("session_id")) != session_id:
                continue
            _ensure_session_defaults_unlocked(talk)
            if not bool(talk.get("user_response_wait_active", False)):
                return None
            if not _clear_due_user_response_wait_unlocked(talk, now=now):
                return None
            ensure_session_storage_unlocked(runtime_root, username=normalized, session=talk)
            return dict(talk)
        return None


def schedule_session_auto_resume(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    reason: str,
    error_text: str = "",
    retry_after_seconds: Any | None = None,
    mark_completed: bool = True,
) -> dict[str, Any] | None:
    normalized = normalize_username(username)
    with state_lock(runtime_root):
        state = _load_state_unlocked(runtime_root)
        if not _ensure_session_exists_unlocked(state, normalized, session_id):
            return None
        for talk in _conversation_sessions(state).get(normalized, []):
            if not isinstance(talk, dict) or str(talk.get("session_id")) != session_id:
                continue
            _ensure_session_defaults_unlocked(talk)
            if not bool(talk.get("auto_resume_enabled", False)):
                talk["auto_resume_next_at"] = ""
                talk["auto_resume_reason"] = ""
                talk["auto_resume_last_error"] = str(error_text or "")
                talk["updated_at"] = utc_ts()
                ensure_session_storage_unlocked(runtime_root, username=normalized, session=talk)
                return dict(talk)
            try:
                delay_seconds = int(
                    retry_after_seconds
                    if retry_after_seconds is not None
                    else talk.get("auto_resume_interval_seconds", DEFAULT_AUTO_RESUME_INTERVAL_SECONDS)
                )
            except (TypeError, ValueError):
                delay_seconds = DEFAULT_AUTO_RESUME_INTERVAL_SECONDS
            talk["auto_resume_next_at"] = _utc_ts_after_seconds(delay_seconds)
            talk["auto_resume_reason"] = str(reason or "").strip()
            talk["auto_resume_last_error"] = str(error_text or "").strip()
            talk["auto_resume_last_scheduled_at"] = utc_ts()
            if mark_completed and bool(talk.get("goal_active", False)):
                talk["goal_completed"] = True
                talk["goal_progress_state"] = "complete"
            talk["updated_at"] = utc_ts()
            ensure_session_storage_unlocked(runtime_root, username=normalized, session=talk)
            return dict(talk)
        return None


def consume_session_due_auto_resume(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
) -> dict[str, Any] | None:
    normalized = normalize_username(username)
    with state_lock(runtime_root):
        state = _load_state_unlocked(runtime_root)
        if not _ensure_session_exists_unlocked(state, normalized, session_id):
            return None
        now = datetime.now(UTC)
        for talk in _conversation_sessions(state).get(normalized, []):
            if not isinstance(talk, dict) or str(talk.get("session_id")) != session_id:
                continue
            _ensure_session_defaults_unlocked(talk)
            if not bool(talk.get("auto_resume_enabled", False)):
                return None
            if not bool(talk.get("goal_active", False)) or not bool(talk.get("goal_completed", False)):
                return None
            if not session_operation_allowed(talk, "auto_resume"):
                return None
            due_at = _parse_utc_ts(talk.get("auto_resume_next_at"))
            if due_at is None or due_at > now:
                return None
            target_revision = _active_goal_revision_unlocked(talk)
            if isinstance(target_revision, dict):
                target_revision["goal_completed"] = False
                target_revision["goal_progress_state"] = "in_progress"
                target_revision["updated_at"] = utc_ts()
            talk["goal_completed"] = False
            talk["goal_progress_state"] = "in_progress"
            talk["auto_resume_last_started_at"] = utc_ts()
            talk["auto_resume_next_at"] = ""
            talk["auto_resume_reason"] = ""
            _apply_active_goal_snapshot_unlocked(talk)
            talk["updated_at"] = utc_ts()
            ensure_session_storage_unlocked(runtime_root, username=normalized, session=talk)
            if isinstance(target_revision, dict):
                write_goal_dir(runtime_root, username=normalized, session_id=session_id, revision=target_revision)
            return dict(talk)
        return None


def update_session_context_status(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    context_status: dict[str, Any] | None,
) -> dict[str, Any] | None:
    normalized = normalize_username(username)
    with state_lock(runtime_root):
        state = _load_state_unlocked(runtime_root)
        if not _ensure_session_exists_unlocked(state, normalized, session_id):
            return None
        for talk in _conversation_sessions(state).get(normalized, []):
            if isinstance(talk, dict) and str(talk.get("session_id")) == session_id:
                _ensure_session_defaults_unlocked(talk)
                talk["last_context_status"] = dict(context_status) if isinstance(context_status, dict) else None
                talk["last_context_status_updated_at"] = utc_ts()
                talk["updated_at"] = utc_ts()
                ensure_session_storage_unlocked(runtime_root, username=normalized, session=talk)
                return dict(talk)
        return None


def record_session_agent_contact(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    service_id: str,
    agent_id: str | None = None,
    provider: str | None = None,
    join_role: str | None = None,
    join_transport: str | None = None,
    turn_completed_at: str | None = None,
) -> dict[str, Any] | None:
    normalized = normalize_username(username)
    normalized_service_id = str(service_id or "").strip()
    normalized_agent_id = str(agent_id or "").strip()
    if not normalized_service_id:
        return None
    normalized_provider = str(provider or "").strip().lower()
    normalized_join_role = str(join_role or "").strip().lower()
    if (
        not normalized_agent_id
        and normalized_join_role in {"interactive_agent", "worker_agent", "goal_manager"}
    ):
        normalized_agent_id = f"{normalized_service_id}@@{session_id}@@{normalized_join_role}"
    with state_lock(runtime_root):
        state = _load_state_unlocked(runtime_root)
        if not _ensure_session_exists_unlocked(state, normalized, session_id):
            return None
        for talk in _conversation_sessions(state).get(normalized, []):
            if isinstance(talk, dict) and str(talk.get("session_id")) == session_id:
                _ensure_session_defaults_unlocked(talk)
                welcomed_agents = talk.get("welcomed_agents")
                if not isinstance(welcomed_agents, list):
                    welcomed_agents = []
                native_provider_kinds = set(NATIVE_PROVIDER_KINDS)
                is_native_contact = (
                    normalized_provider in native_provider_kinds
                    or any(normalized_service_id.startswith(f"service-{kind}-") for kind in native_provider_kinds)
                )
                if is_native_contact:
                    welcomed_agents = [
                        item
                        for item in welcomed_agents
                        if not (
                                isinstance(item, dict)
                                and (
                                    str(item.get("join_role") or "").strip().lower() == normalized_join_role
                                    and
                                    (
                                    str(item.get("provider") or "").strip().lower() in native_provider_kinds
                                    or any(
                                        str(item.get("service_id") or "").strip().startswith(f"service-{kind}-")
                                        for kind in native_provider_kinds
                                    )
                                    )
                                )
                            )
                        ]
                existing: dict[str, Any] | None = None
                for item in welcomed_agents:
                    if not isinstance(item, dict):
                        continue
                    item_service_id = str(item.get("service_id") or "").strip()
                    item_agent_id = str(item.get("agent_id") or "").strip()
                    item_role = str(item.get("join_role") or "").strip().lower()
                    if normalized_agent_id and item_agent_id == normalized_agent_id:
                        existing = item
                        break
                    if (
                        not normalized_agent_id
                        and item_service_id == normalized_service_id
                        and item_role == normalized_join_role
                    ):
                        existing = item
                        break
                now = utc_ts()
                if existing is None:
                    if not normalized_agent_id:
                        normalized_agent_id = f"{normalized_service_id}@@{session_id}"
                    existing = {
                        "agent_id": normalized_agent_id,
                        "service_id": normalized_service_id,
                        "provider": normalized_provider,
                        "welcomed_at": now,
                        "last_turn_completed_at": "",
                        "updated_at": now,
                    }
                    welcomed_agents.append(existing)
                    # Ensure per-agent file directories exist (inbox + outbox)
                    session_agent_inbox_dir(runtime_root, username=normalized, session_id=session_id, agent_id=normalized_agent_id).mkdir(parents=True, exist_ok=True)
                    session_agent_outbox_dir(runtime_root, username=normalized, session_id=session_id, agent_id=normalized_agent_id).mkdir(parents=True, exist_ok=True)
                else:
                    existing["updated_at"] = now
                    if not str(existing.get("agent_id") or "").strip():
                        existing["agent_id"] = normalized_agent_id or f"{normalized_service_id}@@{session_id}"
                if normalized_provider:
                    existing["provider"] = normalized_provider
                if normalized_join_role:
                    existing["join_role"] = normalized_join_role
                normalized_join_transport = str(join_transport or "").strip().lower()
                if normalized_join_transport:
                    existing["join_transport"] = normalized_join_transport
                existing.setdefault("joined_at", existing.get("welcomed_at") or now)
                if isinstance(turn_completed_at, str) and turn_completed_at.strip():
                    existing["last_turn_completed_at"] = turn_completed_at.strip()
                talk["welcomed_agents"] = welcomed_agents
                talk["updated_at"] = now
                ensure_session_storage_unlocked(runtime_root, username=normalized, session=talk)
                return dict(talk)
        return None


def join_session_agent(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    service_id: str,
    agent_id: str | None = None,
    provider: str | None = None,
    role: str = "agent",
    transport: str = "local",
    turn_completed_at: str | None = None,
) -> dict[str, Any] | None:
    """Join a local or remote agent-like participant to a session.

    This is the common session-membership primitive used by local LLM workers,
    GoalManager workers, and WS peers.  ``record_session_agent_contact`` remains
    the low-level persistence helper for compatibility with older call sites.
    """
    normalized_role = str(role or "agent").strip().lower() or "agent"
    normalized_transport = str(transport or "local").strip().lower() or "local"
    return record_session_agent_contact(
        runtime_root,
        username=username,
        session_id=session_id,
        service_id=service_id,
        agent_id=agent_id,
        provider=provider,
        join_role=normalized_role,
        join_transport=normalized_transport,
        turn_completed_at=turn_completed_at,
    )


def resolve_session_agent_id(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    service_id: str,
    role: str | None = None,
) -> str:
    normalized_service_id = str(service_id or "").strip()
    if not normalized_service_id:
        return ""
    normalized_role = str(role or "").strip().lower()
    role_agent_id = (
        f"{normalized_service_id}@@{session_id}@@{normalized_role}"
        if normalized_role in {"interactive_agent", "worker_agent", "goal_manager"}
        else ""
    )
    welcomed_agents = list_session_agent_contacts(runtime_root, username=username, session_id=session_id)
    if role_agent_id:
        for item in welcomed_agents:
            if (
                str(item.get("service_id") or "").strip() == normalized_service_id
                and (
                    str(item.get("agent_id") or "").strip() == role_agent_id
                    or str(item.get("join_role") or "").strip().lower() == normalized_role
                )
            ):
                agent_id = str(item.get("agent_id") or "").strip()
                if agent_id:
                    return agent_id
        return role_agent_id
    for item in welcomed_agents:
        if str(item.get("service_id") or "").strip() == normalized_service_id:
            agent_id = str(item.get("agent_id") or "").strip()
            if agent_id:
                return agent_id
    return f"{normalized_service_id}@@{session_id}"


def list_session_agent_contacts(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
) -> list[dict[str, Any]]:
    talk = get_session_settings(runtime_root, username=username, session_id=session_id)
    if not isinstance(talk, dict):
        return []
    welcomed_agents = talk.get("welcomed_agents")
    if not isinstance(welcomed_agents, list):
        return []
    return [dict(item) for item in welcomed_agents if isinstance(item, dict)]


def update_goal_manager_review_cursor(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    last_turn_completed_at: str,
) -> dict[str, Any] | None:
    normalized = normalize_username(username)
    with state_lock(runtime_root):
        state = _load_state_unlocked(runtime_root)
        if not _ensure_session_exists_unlocked(state, normalized, session_id):
            return None
        for talk in _conversation_sessions(state).get(normalized, []):
            if isinstance(talk, dict) and str(talk.get("session_id")) == session_id:
                _ensure_session_defaults_unlocked(talk)
                talk["goal_manager_last_reviewed_turn_completed_at"] = str(last_turn_completed_at or "").strip()
                talk["updated_at"] = utc_ts()
                ensure_session_storage_unlocked(runtime_root, username=normalized, session=talk)
                goal_manager_state_path = session_goal_manager_state_path(
                    runtime_root,
                    username=normalized,
                    session_id=session_id,
                )
                goal_manager_state = read_json_file(goal_manager_state_path) or {}
                review_cursor = talk["goal_manager_last_reviewed_turn_completed_at"]
                goal_manager_state["last_reviewed_turn_completed_at"] = review_cursor
                pending_work_items = goal_manager_state.get("pending_work_items")
                if isinstance(pending_work_items, list):
                    remaining_pending_work_items = []
                    for item in pending_work_items:
                        if not isinstance(item, dict):
                            remaining_pending_work_items.append(item)
                            continue
                        item_kind = str(item.get("kind") or "").strip().lower()
                        item_ts = str(item.get("ts") or "").strip()
                        if item_kind == "turn_completed" and item_ts and item_ts <= review_cursor:
                            continue
                        remaining_pending_work_items.append(item)
                    goal_manager_state["pending_work_items"] = remaining_pending_work_items
                    if not remaining_pending_work_items:
                        runtime_state = str(goal_manager_state.get("state") or "").strip().lower()
                        if runtime_state in {"queued", "running"}:
                            goal_manager_state["state"] = "idle"
                        goal_manager_state.pop("stale_reason", None)
                queued_turn_completed_at = str(
                    goal_manager_state.get("last_queued_turn_completed_at") or ""
                ).strip()
                if queued_turn_completed_at and queued_turn_completed_at <= review_cursor:
                    goal_manager_state["last_queued_turn_completed_at"] = ""
                goal_manager_state["updated_at"] = talk["updated_at"]
                write_json_file(goal_manager_state_path, goal_manager_state)
                return dict(talk)
        return None


def rename_session(runtime_root: Path, *, username: str, session_id: str, label: str) -> dict[str, Any] | None:
    normalized = normalize_username(username)
    normalized_label = str(label or "").strip()
    if not normalized_label:
        return None
    with state_lock(runtime_root):
        state = _load_state_unlocked(runtime_root)
        if not _ensure_session_exists_unlocked(state, normalized, session_id):
            return None
        for talk in _conversation_sessions(state).get(normalized, []):
            if isinstance(talk, dict) and str(talk.get("session_id")) == session_id:
                _ensure_session_defaults_unlocked(talk)
                talk["label"] = normalized_label
                talk["updated_at"] = utc_ts()
                ensure_session_storage_unlocked(runtime_root, username=normalized, session=talk)
                return dict(talk)
        return None


def select_session(runtime_root: Path, *, token: str, session_id: str) -> dict[str, str] | None:
    token_hash = digest_token(token)
    with state_lock(runtime_root):
        state = _load_state_unlocked(runtime_root)
        session = _auth_sessions(state).get(token_hash)
        if not session:
            return None
        username = str(session["username"])
        if not _ensure_session_exists_unlocked(state, username, session_id):
            return None
        session["active_session_id"] = session_id
        session["updated_at"] = utc_ts()
        write_state(runtime_root, state)
        return {"username": username, "session_id": session_id}


def _load_dag_ids(path: Path, key: str) -> list[str]:
    payload = read_json_file(path) or {}
    values = payload.get(key)
    if not isinstance(values, list):
        return []
    return [str(item).strip() for item in values if str(item).strip()]


def _write_dag_ids(path: Path, key: str, values: list[str]) -> None:
    write_json_file(path, {key: values})


def _list_session_parents_unlocked(runtime_root: Path, *, username: str, session_id: str) -> list[str]:
    return _load_dag_ids(
        session_dag_parents_path(runtime_root, username=username, session_id=session_id),
        "parents",
    )


def _list_session_children_unlocked(runtime_root: Path, *, username: str, session_id: str) -> list[str]:
    return _load_dag_ids(
        session_dag_children_path(runtime_root, username=username, session_id=session_id),
        "children",
    )


def list_session_parents(runtime_root: Path, *, username: str, session_id: str) -> list[str]:
    normalized = normalize_username(username)
    with state_read_lock(runtime_root):
        return _list_session_parents_unlocked(runtime_root, username=normalized, session_id=session_id)


def list_session_children(runtime_root: Path, *, username: str, session_id: str) -> list[str]:
    normalized = normalize_username(username)
    with state_read_lock(runtime_root):
        return _list_session_children_unlocked(runtime_root, username=normalized, session_id=session_id)


def _session_descends_from(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    ancestor_session_id: str,
) -> bool:
    if session_id == ancestor_session_id:
        return True
    seen: set[str] = set()
    stack = [session_id]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        parents = _list_session_parents_unlocked(runtime_root, username=username, session_id=current)
        if ancestor_session_id in parents:
            return True
        stack.extend(parents)
    return False


def _list_active_in_progress_child_sessions_unlocked(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
) -> list[str]:
    active_children: list[str] = []
    for child_session_id in _list_session_children_unlocked(
        runtime_root,
        username=username,
        session_id=session_id,
    ):
        child_session = read_json_file(
            session_metadata_path(runtime_root, username=username, session_id=child_session_id)
        ) or {}
        _ensure_session_defaults_unlocked(child_session)
        child_goal_active = bool(child_session.get("goal_active", False))
        child_goal_progress_state = str(
            child_session.get(
                "goal_progress_state",
                "complete" if bool(child_session.get("goal_completed", False)) else "in_progress",
            )
        ).strip().lower()
        if child_goal_active and child_goal_progress_state == "in_progress":
            active_children.append(child_session_id)
    return active_children


def list_active_in_progress_child_sessions(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
) -> list[str]:
    normalized = normalize_username(username)
    with state_read_lock(runtime_root):
        return _list_active_in_progress_child_sessions_unlocked(
            runtime_root,
            username=normalized,
            session_id=session_id,
        )


def _parent_resumed_after_recovery_created_unlocked(
    runtime_root: Path,
    *,
    username: str,
    parent_session_id: str,
    recovery_session: dict[str, Any],
) -> bool:
    recovery_created_at = _parse_utc_ts(recovery_session.get("created_at"))
    if recovery_created_at is None:
        return False
    parent_history = read_jsonl(
        session_timeline_path(runtime_root, username=username, session_id=parent_session_id)
    )
    for entry in parent_history:
        entry_ts = _parse_utc_ts((entry or {}).get("ts"))
        if entry_ts is None or entry_ts <= recovery_created_at:
            continue
        direction = str((entry or {}).get("direction") or "")
        event_type = str((entry or {}).get("event_type") or "")
        if direction == "in":
            return True
        if event_type in {
            "agent.turn_started",
            "turn.completed",
            "service.panic_cleared_after_successful_turn",
        }:
            return True
    return False


def _complete_recovery_session_unlocked(
    runtime_root: Path,
    *,
    username: str,
    recovery_session: dict[str, Any],
    completed_at: str,
    reason: str,
) -> dict[str, Any]:
    _ensure_session_defaults_unlocked(recovery_session)
    recovery_session["goal_active"] = False
    recovery_session["goal_completed"] = True
    recovery_session["goal_progress_state"] = "complete"
    recovery_session["recovery_auto_completed_at"] = completed_at
    recovery_session["recovery_auto_completed_reason"] = reason
    recovery_session["updated_at"] = completed_at
    active_revision = _active_goal_revision_unlocked(recovery_session)
    if active_revision is not None:
        active_revision["goal_active"] = False
        active_revision["goal_completed"] = True
        active_revision["goal_progress_state"] = "complete"
        active_revision["updated_at"] = completed_at
    _apply_active_goal_snapshot_unlocked(recovery_session)
    ensure_session_storage_unlocked(runtime_root, username=username, session=recovery_session)
    if active_revision is not None:
        write_goal_dir(
            runtime_root,
            username=username,
            session_id=str(recovery_session.get("session_id") or ""),
            revision=active_revision,
        )
    return recovery_session


def _complete_resumed_recovery_children_unlocked(
    runtime_root: Path,
    *,
    username: str,
    parent_session_id: str,
) -> list[str]:
    completed: list[str] = []
    now = utc_ts()
    for child_session_id in _list_session_children_unlocked(
        runtime_root,
        username=username,
        session_id=parent_session_id,
    ):
        child_session = read_json_file(
            session_metadata_path(runtime_root, username=username, session_id=child_session_id)
        ) or {}
        _ensure_session_defaults_unlocked(child_session)
        if str(child_session.get("session_group") or "").strip().lower() != "error":
            continue
        child_source_session_id = str(
            child_session.get("recovery_source_session_id")
            or child_session.get("source_session_id")
            or child_session.get("parent_session_id")
            or ""
        ).strip()
        if child_source_session_id != parent_session_id:
            continue
        child_goal_active = bool(child_session.get("goal_active", False))
        child_goal_progress_state = str(
            child_session.get(
                "goal_progress_state",
                "complete" if bool(child_session.get("goal_completed", False)) else "in_progress",
            )
        ).strip().lower()
        if not (child_goal_active and child_goal_progress_state == "in_progress"):
            continue
        if not _parent_resumed_after_recovery_created_unlocked(
            runtime_root,
            username=username,
            parent_session_id=parent_session_id,
            recovery_session=child_session,
        ):
            continue
        _complete_recovery_session_unlocked(
            runtime_root,
            username=username,
            recovery_session=child_session,
            completed_at=now,
            reason="parent_resumed_after_recovery_creation",
        )
        completed.append(child_session_id)
    return completed


def supersede_recovery_child_sessions(
    runtime_root: Path,
    *,
    username: str,
    parent_session_id: str,
    keep_session_id: str,
) -> dict[str, Any] | None:
    normalized = normalize_username(username)
    if not keep_session_id:
        return None
    now = utc_ts()
    superseded_children: list[str] = []
    for child_session_id in list_session_children(
        runtime_root,
        username=normalized,
        session_id=parent_session_id,
    ):
        if child_session_id == keep_session_id:
            continue
        child_session = get_session_settings(
            runtime_root,
            username=normalized,
            session_id=child_session_id,
        )
        if not isinstance(child_session, dict):
            continue
        if str(child_session.get("session_group") or "").strip().lower() != "error":
            continue
        child_source_session_id = str(
            child_session.get("recovery_source_session_id")
            or child_session.get("source_session_id")
            or child_session.get("parent_session_id")
            or ""
        ).strip()
        if child_source_session_id != parent_session_id:
            continue
        child_goal_active = bool(child_session.get("goal_active", False))
        child_goal_progress_state = str(
            child_session.get(
                "goal_progress_state",
                "complete" if bool(child_session.get("goal_completed", False)) else "in_progress",
            )
        ).strip().lower()
        if not (child_goal_active and child_goal_progress_state == "in_progress"):
            continue
        updated = update_session_goal_flags(
            runtime_root,
            username=normalized,
            session_id=child_session_id,
            goal_active=False,
            goal_completed=True,
            goal_progress_state="complete",
        )
        child_session = updated or child_session
        child_session["recovery_superseded_at"] = now
        child_session["recovery_superseded_by_session_id"] = keep_session_id
        child_session["updated_at"] = now
        write_json_file(
            session_metadata_path(runtime_root, username=normalized, session_id=child_session_id),
            child_session,
        )
        superseded_children.append(child_session_id)
    remaining_children = list_active_in_progress_child_sessions(
        runtime_root,
        username=normalized,
        session_id=parent_session_id,
    )
    parent_session = get_session_settings(
        runtime_root,
        username=normalized,
        session_id=parent_session_id,
    ) or {}
    parent_session["waiting_on_children"] = bool(remaining_children)
    parent_session["updated_at"] = now
    write_json_file(
        session_metadata_path(runtime_root, username=normalized, session_id=parent_session_id),
        parent_session,
    )
    return {
        "parent_session_id": parent_session_id,
        "keep_session_id": keep_session_id,
        "superseded_children": superseded_children,
        "waiting_on_children": bool(remaining_children),
        "remaining_children": remaining_children,
    }


def add_session_child(
    runtime_root: Path,
    *,
    username: str,
    parent_session_id: str,
    child_session_id: str,
) -> dict[str, list[str]] | None:
    normalized = normalize_username(username)
    if not child_session_id or parent_session_id == child_session_id:
        return None
    with state_lock(runtime_root):
        state = _load_state_unlocked(runtime_root)
        if not _ensure_session_exists_unlocked(state, normalized, parent_session_id):
            return None
        if not _ensure_session_exists_unlocked(state, normalized, child_session_id):
            return None
        if _session_descends_from(
            runtime_root,
            username=normalized,
            session_id=parent_session_id,
            ancestor_session_id=child_session_id,
        ):
            raise ValueError("session_dag_cycle")
        parent_children = _list_session_children_unlocked(
            runtime_root,
            username=normalized,
            session_id=parent_session_id,
        )
        if child_session_id not in parent_children:
            parent_children.append(child_session_id)
        _write_dag_ids(
            session_dag_children_path(runtime_root, username=normalized, session_id=parent_session_id),
            "children",
            parent_children,
        )
        child_parents = _list_session_parents_unlocked(
            runtime_root,
            username=normalized,
            session_id=child_session_id,
        )
        if parent_session_id not in child_parents:
            child_parents.append(parent_session_id)
        _write_dag_ids(
            session_dag_parents_path(runtime_root, username=normalized, session_id=child_session_id),
            "parents",
            child_parents,
        )
        parent_session = read_json_file(
            session_metadata_path(runtime_root, username=normalized, session_id=parent_session_id)
        ) or {}
        parent_session["waiting_on_children"] = bool(
            _list_active_in_progress_child_sessions_unlocked(
                runtime_root,
                username=normalized,
                session_id=parent_session_id,
            )
        )
        parent_session["updated_at"] = utc_ts()
        ensure_session_storage_unlocked(runtime_root, username=normalized, session=parent_session)
        child_session = read_json_file(
            session_metadata_path(runtime_root, username=normalized, session_id=child_session_id)
        ) or {}
        child_session["parent_session_id"] = parent_session_id
        child_session["updated_at"] = utc_ts()
        ensure_session_storage_unlocked(runtime_root, username=normalized, session=child_session)
        return {"parents": child_parents, "children": parent_children}


def create_child_conversation_session(
    runtime_root: Path,
    *,
    username: str,
    parent_session_id: str,
    label: str | None = None,
    goal_text: str | None = None,
    session_group: str | None = None,
    session_permissions: dict[str, Any] | None = None,
    created_by_username: str | None = None,
    created_by_type: str | None = None,
    origin_session_id: str | None = None,
    origin_goal_id: str | None = None,
    origin_goal_text: str | None = None,
    session_ui_mode: str | None = None,
    session_interactive: bool = False,
    communication_agent_enabled: bool = False,
    communication_agent_priority: list[str] | None = None,
    child_session_sharing: dict[str, Any] | None = None,
    session_skills: list[dict[str, Any]] | None = None,
    requester_session_id: str | None = None,
    requester_unit_id: str | None = None,
    requester_template_id: str | None = None,
) -> dict[str, Any] | None:
    normalized = normalize_username(username)
    parent = get_session_settings(runtime_root, username=username, session_id=parent_session_id)
    if not isinstance(parent, dict):
        return None
    system_root_unit_child = (
        str(parent.get("session_group") or "").strip().lower() == "root"
        and str(created_by_type or "").strip().lower() == "unit"
        and bool(str(requester_unit_id or requester_template_id or "").strip())
    )
    if not session_allows_child_session_creator(
        parent,
        requester_session_id=requester_session_id or origin_session_id or parent_session_id,
        requester_unit_id=requester_unit_id,
        requester_template_id=requester_template_id,
    ) and not system_root_unit_child:
        return None
    child = create_conversation_session(
        runtime_root,
        username=username,
        label=label or "Subgoal",
        session_group=session_group,
        session_permissions=session_permissions,
        created_by_username=created_by_username or normalized,
        created_by_type=created_by_type or "user",
        origin_session_id=origin_session_id or parent_session_id,
        origin_goal_id=origin_goal_id or str(parent.get("active_goal_id") or parent.get("goal_id") or "").strip(),
        origin_goal_text=origin_goal_text if origin_goal_text is not None else str(parent.get("goal_text") or ""),
        session_ui_mode=session_ui_mode,
        session_interactive=session_interactive,
        communication_agent_enabled=communication_agent_enabled,
        communication_agent_priority=communication_agent_priority,
        child_session_sharing=child_session_sharing,
        session_skills=session_skills,
    )
    if goal_text:
        update_session_goal(
            runtime_root,
            username=username,
            session_id=str(child["session_id"]),
            goal_text=goal_text,
            updated_by_username=created_by_username or normalized,
            updated_by_type=created_by_type or "user",
            origin_session_id=origin_session_id or parent_session_id,
            origin_goal_id=origin_goal_id or str(parent.get("active_goal_id") or parent.get("goal_id") or "").strip(),
            origin_goal_text=origin_goal_text if origin_goal_text is not None else str(parent.get("goal_text") or ""),
        )
    add_session_child(
        runtime_root,
        username=username,
        parent_session_id=parent_session_id,
        child_session_id=str(child["session_id"]),
    )
    return get_session_settings(runtime_root, username=username, session_id=str(child["session_id"]))


def update_session_child_sharing(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    child_session_sharing: dict[str, Any] | None,
) -> dict[str, Any] | None:
    normalized = normalize_username(username)
    policy = normalize_child_session_sharing_policy(child_session_sharing)
    with state_lock(runtime_root):
        state = _load_state_unlocked(runtime_root)
        if not _ensure_session_exists_unlocked(state, normalized, session_id):
            return None
        for talk in _conversation_sessions(state).get(normalized, []):
            if isinstance(talk, dict) and str(talk.get("session_id") or "") == session_id:
                _ensure_session_defaults_unlocked(talk)
                talk["child_session_sharing"] = policy
                talk["updated_at"] = utc_ts()
                ensure_session_storage_unlocked(runtime_root, username=normalized, session=talk)
                write_state(runtime_root, state)
                return dict(talk)
    return None


def update_session_skills(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    session_skills: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    normalized = normalize_username(username)
    normalized_skills = normalize_session_skills(session_skills)
    with state_lock(runtime_root):
        state = _load_state_unlocked(runtime_root)
        if not _ensure_session_exists_unlocked(state, normalized, session_id):
            return None
        for talk in _conversation_sessions(state).get(normalized, []):
            if isinstance(talk, dict) and str(talk.get("session_id") or "") == session_id:
                _ensure_session_defaults_unlocked(talk)
                talk["session_skills"] = normalized_skills
                talk["updated_at"] = utc_ts()
                ensure_session_storage_unlocked(runtime_root, username=normalized, session=talk)
                write_state(runtime_root, state)
                return dict(talk)
    return None


def session_goal_context(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
) -> list[dict[str, str]]:
    normalized = normalize_username(username)
    with state_read_lock(runtime_root):
        seen_sessions: set[str] = set()

        def _load_session(session_key: str) -> dict[str, Any]:
            session = read_json_file(
                session_metadata_path(runtime_root, username=normalized, session_id=session_key)
            ) or {}
            _ensure_session_defaults_unlocked(session)
            return session

        def _root_ancestors(session_key: str) -> list[str]:
            if session_key in seen_sessions:
                return []
            seen_sessions.add(session_key)
            parents = _list_session_parents_unlocked(runtime_root, username=normalized, session_id=session_key)
            if not parents:
                return [session_key]
            roots: list[str] = []
            for parent_id in parents:
                for root_id in _root_ancestors(parent_id):
                    if root_id not in roots:
                        roots.append(root_id)
            return roots

        current_session = _load_session(session_id)
        root_limit = max(1, int(current_session.get("goal_context_root_limit", 2) or 2))
        recent_limit = max(1, int(current_session.get("goal_context_recent_limit", 2) or 2))
        current_history = current_session.get("goal_history")
        current_revisions = (
            [item for item in current_history if isinstance(item, dict)]
            if isinstance(current_history, list)
            else []
        )
        root_revision_candidates: list[dict[str, Any]] = []
        for root_session_id in _root_ancestors(session_id):
            root_session = _load_session(root_session_id)
            root_history = root_session.get("goal_history")
            if not isinstance(root_history, list):
                continue
            for revision in root_history:
                if isinstance(revision, dict):
                    root_revision_candidates.append(revision)

        selected_revisions = root_revision_candidates[:root_limit] + current_revisions[-recent_limit:]
        seen_goal_ids: set[str] = set()
        context: list[dict[str, str]] = []
        for revision in selected_revisions:
            goal_id = str(revision.get("goal_id") or "").strip()
            goal_text = str(revision.get("goal_text") or "").strip()
            if not goal_id or not goal_text or goal_id in seen_goal_ids:
                continue
            seen_goal_ids.add(goal_id)
            context.append(
                {
                    "goal_id": goal_id,
                    "goal_text": goal_text,
                    "goal_created_at": str(revision.get("created_at") or "").strip(),
                }
            )
        return context


def complete_session_child(
    runtime_root: Path,
    *,
    username: str,
    parent_session_id: str,
    child_session_id: str,
) -> dict[str, Any] | None:
    normalized = normalize_username(username)
    with state_lock(runtime_root):
        state = _load_state_unlocked(runtime_root)
        if not _ensure_session_exists_unlocked(state, normalized, parent_session_id):
            return None
        if not _ensure_session_exists_unlocked(state, normalized, child_session_id):
            return None
        child_parents = _list_session_parents_unlocked(
            runtime_root,
            username=normalized,
            session_id=child_session_id,
        )
        if parent_session_id not in child_parents:
            return None
        child_session = read_json_file(
            session_metadata_path(runtime_root, username=normalized, session_id=child_session_id)
        ) or {}
        child_session["goal_completed"] = True
        child_session["goal_progress_state"] = "complete"
        child_session["child_completion_reported_at"] = utc_ts()
        child_session["updated_at"] = utc_ts()
        ensure_session_storage_unlocked(runtime_root, username=normalized, session=child_session)
        remaining_children = _list_active_in_progress_child_sessions_unlocked(
            runtime_root,
            username=normalized,
            session_id=parent_session_id,
        )
        parent_session = read_json_file(
            session_metadata_path(runtime_root, username=normalized, session_id=parent_session_id)
        ) or {}
        parent_session["waiting_on_children"] = bool(remaining_children)
        parent_session["updated_at"] = utc_ts()
        ensure_session_storage_unlocked(runtime_root, username=normalized, session=parent_session)
        return {
            "parent_session_id": parent_session_id,
            "child_session_id": child_session_id,
            "waiting_on_children": bool(remaining_children),
            "remaining_children": remaining_children,
        }


def update_session_peer_joinable(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    peer_joinable: bool,
) -> dict[str, Any] | None:
    """Set or clear the peer_joinable flag on a session.

    When ``peer_joinable`` is ``True`` the session is visible to remote AIze
    peers connecting via ``/ws`` and they may join it as external agents.
    """
    normalized = normalize_username(username)
    with state_lock(runtime_root):
        state = _load_state_unlocked(runtime_root)
        if not _ensure_session_exists_unlocked(state, normalized, session_id):
            return None
        for talk in _conversation_sessions(state).get(normalized, []):
            if isinstance(talk, dict) and str(talk.get("session_id")) == session_id:
                _ensure_session_defaults_unlocked(talk)
                talk["peer_joinable"] = bool(peer_joinable)
                talk["updated_at"] = utc_ts()
                ensure_session_storage_unlocked(runtime_root, username=normalized, session=talk)
                return dict(talk)
    return None


def update_session_selected_agents(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    selected_agents: list[str],
) -> dict[str, Any] | None:
    """Set the active-agent list for a session.

    ``selected_agents`` is a list of service_id strings or the special tokens
    ``"codex_pool"`` and ``"claude_pool"``.  When it contains only WS-peer
    service_ids (no pool token) the HTTP prompt dispatch skips the local LLM
    workers and lets the subscribed WS peer respond via the event pump.
    """
    normalized = normalize_username(username)
    with state_lock(runtime_root):
        state = _load_state_unlocked(runtime_root)
        if not _ensure_session_exists_unlocked(state, normalized, session_id):
            return None
        for talk in _conversation_sessions(state).get(normalized, []):
            if isinstance(talk, dict) and str(talk.get("session_id")) == session_id:
                _ensure_session_defaults_unlocked(talk)
                talk["selected_agents"] = [str(a) for a in selected_agents if a]
                talk["updated_at"] = utc_ts()
                ensure_session_storage_unlocked(runtime_root, username=normalized, session=talk)
                return dict(talk)
    return None


def update_session_launcher_profile(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    launcher_template_id: str,
    launcher_display_name: str,
    preferred_provider: str,
    selected_agents: list[str],
    service_targets: list[dict[str, str]],
    launcher_unit_kind: str = "",
    launcher_unit_class: str = "",
    launcher_instance_policy: str = "",
    workspace_scope: str = "none",
    workspace_path: str = "",
    goal_completion_policy: str = "standard",
) -> dict[str, Any] | None:
    normalized = normalize_username(username)
    with state_lock(runtime_root):
        state = _load_state_unlocked(runtime_root)
        if not _ensure_session_exists_unlocked(state, normalized, session_id):
            return None
        for talk in _conversation_sessions(state).get(normalized, []):
            if isinstance(talk, dict) and str(talk.get("session_id")) == session_id:
                _ensure_session_defaults_unlocked(talk)
                launcher_unit_id = str(launcher_template_id or "").strip()
                talk["launcher_unit_id"] = launcher_unit_id
                talk["launcher_template_id"] = launcher_unit_id
                talk["launcher_display_name"] = str(launcher_display_name or "").strip()
                talk["launcher_unit_kind"] = str(launcher_unit_kind or "").strip().lower()
                talk["launcher_unit_class"] = str(launcher_unit_class or "").strip().lower()
                talk["launcher_instance_policy"] = str(launcher_instance_policy or "").strip().lower()
                talk["launcher_preferred_provider"] = str(preferred_provider or "").strip().lower()
                talk["launcher_selected_agents"] = [str(agent) for agent in selected_agents if str(agent).strip()]
                talk["launcher_workspace_scope"] = str(workspace_scope or "none").strip().lower() or "none"
                talk["launcher_workspace_path"] = str(workspace_path or "").strip()
                completion_policy = str(goal_completion_policy or "standard").strip().lower()
                talk["goal_completion_policy"] = (
                    completion_policy
                    if completion_policy in {"standard", "continuous", "per_prompt"}
                    else "standard"
                )
                talk["launcher_service_targets"] = [
                    {
                        "mode": str(target.get("mode") or "").strip(),
                        "provider": str(target.get("provider") or "").strip(),
                        "target": str(target.get("target") or "").strip(),
                    }
                    for target in service_targets
                    if isinstance(target, dict) and str(target.get("target") or "").strip()
                ]
                talk["updated_at"] = utc_ts()
                _ensure_session_defaults_unlocked(talk)
                ensure_session_storage_unlocked(runtime_root, username=normalized, session=talk)
                return dict(talk)
    return None


def list_peer_joinable_sessions(runtime_root: Path) -> list[dict[str, Any]]:
    """Return all sessions across all users that have ``peer_joinable=True``.

    Each entry includes a ``username`` key so remote peers know which user
    owns the session.
    """
    result: list[dict[str, Any]] = []
    sessions_root = sessions_dir(runtime_root)
    with state_read_lock(runtime_root):
        if not sessions_root.exists():
            return result
        for user_dir in sorted(path for path in sessions_root.iterdir() if path.is_dir()):
            username = normalize_username(user_dir.name)
            for session in _list_session_records(runtime_root, username=username):
                if not bool(session.get("peer_joinable")):
                    continue
                entry = {
                    "username": username,
                    "session_id": str(session.get("session_id") or ""),
                    "label": str(session.get("label") or session.get("session_id") or ""),
                    "goal_text": str(session.get("goal_text") or ""),
                    "peer_joinable": True,
                }
                result.append(entry)
    return result
