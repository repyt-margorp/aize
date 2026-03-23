from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import fcntl

from wire.protocol import utc_ts


PBKDF2_ROUNDS = 200_000
DEFAULT_AUTO_COMPACT_THRESHOLD_LEFT_PERCENT = 30
DEFAULT_PENDING_INPUT_LIMIT = 100
DEFAULT_SESSION_GROUP = "user"
DEFAULT_AUTO_RESUME_INTERVAL_SECONDS = 6 * 60 * 60
SESSION_GROUP_DEFAULT_PERMISSIONS = {
    "user": {
        "create_child_session": True,
        "auto_spawn_recovery": True,
        "auto_resume": True,
    },
    "error": {
        "create_child_session": False,
        "auto_spawn_recovery": False,
        "auto_resume": False,
    },
}


def normalize_auto_compact_threshold_left_percent(value: Any) -> int:
    try:
        threshold = int(value)
    except (TypeError, ValueError):
        threshold = DEFAULT_AUTO_COMPACT_THRESHOLD_LEFT_PERCENT
    threshold = max(5, min(95, threshold))
    return threshold - (threshold % 5)


def normalize_username(username: str) -> str:
    return username.strip().lower()


def hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PBKDF2_ROUNDS,
    ).hex()


def digest_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def history_state_key(username: str, session_id: str) -> str:
    return f"{normalize_username(username)}::{session_id}"


def codex_session_key(service_id: str, *, username: str | None, session_id: str | None) -> str:
    if username and session_id:
        return f"{service_id}::{normalize_username(username)}::{session_id}"
    return service_id


def claude_session_key(service_id: str, *, username: str | None, session_id: str | None) -> str:
    if username and session_id:
        return f"{service_id}::{normalize_username(username)}::{session_id}"
    return service_id


def agent_state_key(service_id: str, username: str, session_id: str) -> str:
    return f"{service_id}::{normalize_username(username)}::{session_id}"


def service_pending_state_key(service_id: str, username: str, session_id: str) -> str:
    return f"{service_id}::{normalize_username(username)}::{session_id}"


def state_dir(runtime_root: Path) -> Path:
    return runtime_root.parent / ".aize-state"


def sessions_dir(runtime_root: Path) -> Path:
    return state_dir(runtime_root) / "sessions"


def session_user_dir(runtime_root: Path, *, username: str) -> Path:
    return sessions_dir(runtime_root) / normalize_username(username)


def session_dir(runtime_root: Path, *, username: str, session_id: str) -> Path:
    return session_user_dir(runtime_root, username=username) / session_id


def session_pending_dir(runtime_root: Path, *, username: str, session_id: str) -> Path:
    return session_dir(runtime_root, username=username, session_id=session_id) / "pending"


def session_services_dir(runtime_root: Path, *, username: str, session_id: str) -> Path:
    return session_dir(runtime_root, username=username, session_id=session_id) / "services"


def session_goal_manager_dir(runtime_root: Path, *, username: str, session_id: str) -> Path:
    return session_dir(runtime_root, username=username, session_id=session_id) / "goal_manager"


def session_dag_dir(runtime_root: Path, *, username: str, session_id: str) -> Path:
    return session_dir(runtime_root, username=username, session_id=session_id) / "dag"


def session_metadata_path(runtime_root: Path, *, username: str, session_id: str) -> Path:
    return session_dir(runtime_root, username=username, session_id=session_id) / "session.json"


def session_timeline_path(runtime_root: Path, *, username: str, session_id: str) -> Path:
    return session_dir(runtime_root, username=username, session_id=session_id) / "timeline.jsonl"


def session_pending_path(runtime_root: Path, *, username: str, session_id: str) -> Path:
    return session_pending_dir(runtime_root, username=username, session_id=session_id) / "session.jsonl"


def session_goal_manager_pending_path(runtime_root: Path, *, username: str, session_id: str) -> Path:
    return session_pending_dir(runtime_root, username=username, session_id=session_id) / "goal_manager.jsonl"


def session_agent_pending_path(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    agent_id: str,
) -> Path:
    return session_pending_dir(runtime_root, username=username, session_id=session_id) / "agents" / f"{agent_id}.jsonl"


def session_service_pending_path(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    service_id: str,
) -> Path:
    return session_pending_dir(runtime_root, username=username, session_id=session_id) / "services" / f"{service_id}.jsonl"


def session_service_state_path(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    service_id: str,
) -> Path:
    return session_services_dir(runtime_root, username=username, session_id=session_id) / f"{service_id}.json"


def session_goal_manager_state_path(runtime_root: Path, *, username: str, session_id: str) -> Path:
    return session_goal_manager_dir(runtime_root, username=username, session_id=session_id) / "state.json"


def session_goal_manager_reviews_path(runtime_root: Path, *, username: str, session_id: str) -> Path:
    return session_goal_manager_dir(runtime_root, username=username, session_id=session_id) / "reviews.jsonl"


def session_dag_parents_path(runtime_root: Path, *, username: str, session_id: str) -> Path:
    return session_dag_dir(runtime_root, username=username, session_id=session_id) / "parents.json"


def session_dag_children_path(runtime_root: Path, *, username: str, session_id: str) -> Path:
    return session_dag_dir(runtime_root, username=username, session_id=session_id) / "children.json"


def session_agent_state_path(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    service_id: str,
) -> Path:
    return session_services_dir(runtime_root, username=username, session_id=session_id) / f"{service_id}.audit.json"


def state_path(runtime_root: Path) -> Path:
    return state_dir(runtime_root) / "persistent.json"


def lock_path(runtime_root: Path) -> Path:
    return state_dir(runtime_root) / "persistent.lock"


@contextmanager
def state_lock(runtime_root: Path):
    path = lock_path(runtime_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def state_read_lock(runtime_root: Path):
    """Shared read lock — allows concurrent readers, blocks writers."""
    path = lock_path(runtime_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def write_state(runtime_root: Path, state: dict[str, Any]) -> None:
    path = state_path(runtime_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    persisted_state = dict(state)
    if "_runtime_root" in persisted_state:
        persisted_state["_runtime_root"] = str(persisted_state["_runtime_root"])
    persisted_state.pop("sessions", None)
    persisted_state.pop("talks", None)
    # Session-scoped state is canonical under .aize-state/sessions/... .
    persisted_state.pop("histories", None)
    persisted_state.pop("pending_inputs", None)
    persisted_state.pop("service_pending_inputs", None)
    persisted_state.pop("codex_sessions", None)
    persisted_state.pop("claude_sessions", None)
    persisted_state.pop("conversation_sessions", None)
    persisted_state.pop("agent_states", None)
    fd, temp_path = tempfile.mkstemp(prefix="persistent.", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(persisted_state, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def read_json_file(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else None


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            entry = json.loads(line)
            if isinstance(entry, dict):
                entries.append(entry)
    return entries


def write_jsonl(path: Path, entries: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def remove_file_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def ensure_session_storage_unlocked(
    runtime_root: Path,
    *,
    username: str,
    session: dict[str, Any],
) -> Path:
    normalized = normalize_username(username)
    session_id = str(session.get("session_id") or "").strip()
    directory = session_dir(runtime_root, username=normalized, session_id=session_id)
    directory.mkdir(parents=True, exist_ok=True)
    session_pending_dir(runtime_root, username=normalized, session_id=session_id).mkdir(parents=True, exist_ok=True)
    (session_pending_dir(runtime_root, username=normalized, session_id=session_id) / "services").mkdir(
        parents=True, exist_ok=True
    )
    session_services_dir(runtime_root, username=normalized, session_id=session_id).mkdir(parents=True, exist_ok=True)
    session_goal_manager_dir(runtime_root, username=normalized, session_id=session_id).mkdir(parents=True, exist_ok=True)
    session_dag_dir(runtime_root, username=normalized, session_id=session_id).mkdir(parents=True, exist_ok=True)
    session_payload = dict(session)
    session_payload["_runtime_root"] = str(runtime_root)
    write_json_file(session_metadata_path(runtime_root, username=normalized, session_id=session_id), session_payload)
    goal_manager_state_path = session_goal_manager_state_path(runtime_root, username=normalized, session_id=session_id)
    if not goal_manager_state_path.exists():
        write_json_file(
            goal_manager_state_path,
            {
                "state": "idle",
                "last_reviewed_turn_completed_at": str(session.get("goal_manager_last_reviewed_turn_completed_at") or ""),
                "updated_at": str(session.get("updated_at") or utc_ts()),
            },
        )
    reviews_path = session_goal_manager_reviews_path(runtime_root, username=normalized, session_id=session_id)
    reviews_path.touch(exist_ok=True)
    parents_path = session_dag_parents_path(runtime_root, username=normalized, session_id=session_id)
    if not parents_path.exists():
        write_json_file(parents_path, {"parents": []})
    children_path = session_dag_children_path(runtime_root, username=normalized, session_id=session_id)
    if not children_path.exists():
        write_json_file(children_path, {"children": []})
    return directory


def _load_session_records_for_user_unlocked(runtime_root: Path, username: str) -> list[dict[str, Any]]:
    """Load conversation sessions for one user directly from session files."""
    normalized = normalize_username(username)
    user_dir = session_user_dir(runtime_root, username=normalized)
    if not user_dir.exists():
        return []
    records: list[dict[str, Any]] = []
    for talk_dir in sorted(path for path in user_dir.iterdir() if path.is_dir()):
        session = read_json_file(session_metadata_path(runtime_root, username=normalized, session_id=talk_dir.name))
        if not isinstance(session, dict):
            continue
        if str(session.get("_runtime_root") or "") != str(runtime_root):
            continue
        _ensure_session_defaults_unlocked(session)
        records.append(dict(session))
    return records


def _ensure_user_sessions_cache_unlocked(runtime_root: Path, state: dict[str, Any], username: str) -> list[dict[str, Any]]:
    normalized = normalize_username(username)
    sessions = _conversation_sessions(state).setdefault(normalized, [])
    if not isinstance(sessions, list):
        sessions = []
        _conversation_sessions(state)[normalized] = sessions
    seen: set[str] = set()
    for item in list(sessions):
        if isinstance(item, dict):
            session_id = str(item.get("session_id") or "").strip()
            if session_id:
                seen.add(session_id)
    for session in _load_session_records_for_user_unlocked(runtime_root, normalized):
        session_id = str(session.get("session_id") or "").strip()
        if not session_id or session_id in seen:
            continue
        seen.add(session_id)
        sessions.append(session)
    return sessions


def _auth_sessions(state: dict[str, Any]) -> dict[str, Any]:
    return state.setdefault("auth_sessions", {})


def _conversation_sessions(state: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return state.setdefault("conversation_sessions", {})


def _normalize_goal_revision_unlocked(revision: dict[str, Any], *, fallback_ts: str) -> dict[str, Any]:
    goal_text = str(revision.get("goal_text", "") or "").strip()
    goal_active = bool(revision.get("goal_active", bool(goal_text)))
    progress_state = str(
        revision.get(
            "goal_progress_state",
            "complete" if bool(revision.get("goal_completed", False)) else "in_progress",
        )
    ).strip().lower()
    progress_state = progress_state if progress_state in {"complete", "in_progress"} else "in_progress"
    updated_at = str(revision.get("updated_at") or revision.get("created_at") or fallback_ts)
    return {
        "goal_id": str(revision.get("goal_id") or secrets.token_hex(8)),
        "previous_goal_id": (
            str(revision.get("previous_goal_id") or "").strip() or None
        ),
        "goal_text": goal_text,
        "goal_active": goal_active,
        "goal_completed": progress_state == "complete",
        "goal_progress_state": progress_state,
        "created_at": str(revision.get("created_at") or updated_at),
        "updated_at": updated_at,
    }


def _active_goal_revision_unlocked(session: dict[str, Any]) -> dict[str, Any] | None:
    active_goal_id = str(session.get("active_goal_id") or "").strip()
    history = session.get("goal_history")
    if not isinstance(history, list):
        return None
    for revision in history:
        if isinstance(revision, dict) and str(revision.get("goal_id") or "").strip() == active_goal_id:
            return revision
    return None


def _apply_active_goal_snapshot_unlocked(session: dict[str, Any]) -> None:
    active_revision = _active_goal_revision_unlocked(session)
    if active_revision is None:
        session["goal_id"] = ""
        session["goal_text"] = ""
        session["goal_mode"] = "no_goal"
        session["goal_active"] = False
        session["goal_completed"] = False
        session["goal_progress_state"] = "in_progress"
        session["goal_updated_at"] = session.get("updated_at", utc_ts())
        session.pop("goal_audit_state", None)
        return
    session["goal_id"] = str(active_revision.get("goal_id") or "")
    session["goal_text"] = str(active_revision.get("goal_text", ""))
    session["goal_active"] = bool(active_revision.get("goal_active", False))
    session["goal_completed"] = bool(active_revision.get("goal_completed", False))
    session["goal_progress_state"] = str(active_revision.get("goal_progress_state", "in_progress"))
    session["goal_updated_at"] = str(active_revision.get("updated_at") or session.get("updated_at", utc_ts()))
    session.pop("goal_audit_state", None)
    _normalize_goal_mode_unlocked(session)


def _ensure_goal_history_unlocked(session: dict[str, Any]) -> None:
    fallback_ts = str(session.get("goal_updated_at") or session.get("updated_at") or utc_ts())
    raw_history = session.get("goal_history")
    history: list[dict[str, Any]] = []
    if isinstance(raw_history, list):
        for raw_revision in raw_history:
            if isinstance(raw_revision, dict):
                history.append(_normalize_goal_revision_unlocked(raw_revision, fallback_ts=fallback_ts))
    should_migrate_legacy_goal = bool(str(session.get("goal_text", "") or "").strip()) or bool(
        session.get("goal_completed", False)
    ) or str(session.get("goal_progress_state", "")).strip().lower() == "complete" or bool(
        str(session.get("goal_id") or session.get("active_goal_id") or "").strip()
    )
    if not history and should_migrate_legacy_goal:
        history.append(
            _normalize_goal_revision_unlocked(
                {
                    "goal_id": session.get("goal_id"),
                    "goal_text": session.get("goal_text", ""),
                    "goal_active": session.get("goal_active", False),
                    "goal_completed": session.get("goal_completed", False),
                    "goal_progress_state": session.get("goal_progress_state", "in_progress"),
                    "created_at": session.get("goal_updated_at") or fallback_ts,
                    "updated_at": session.get("goal_updated_at") or fallback_ts,
                },
                fallback_ts=fallback_ts,
            )
        )
    session["goal_history"] = history
    active_goal_id = str(session.get("active_goal_id") or session.get("goal_id") or "").strip()
    if history:
        history_ids = {str(revision.get("goal_id") or "") for revision in history}
        if active_goal_id not in history_ids:
            active_goal_id = str(history[-1].get("goal_id") or "")
    else:
        active_goal_id = ""
    session["active_goal_id"] = active_goal_id
    _apply_active_goal_snapshot_unlocked(session)


def _normalize_goal_mode_unlocked(session: dict[str, Any]) -> None:
    goal_text = str(session.get("goal_text", "") or "").strip()
    if not goal_text:
        session["goal_text"] = ""
        session["goal_mode"] = "no_goal"
        session["goal_active"] = False
        session["goal_completed"] = False
        session["goal_progress_state"] = "in_progress"
        # goal_audit_state is now agent-side; remove stale session-level value when goal is cleared
        session.pop("goal_audit_state", None)
        return
    goal_active = bool(session.get("goal_active", True))
    session["goal_mode"] = "active" if goal_active else "inactive"
    session["goal_active"] = goal_active
    progress_state = str(session.get("goal_progress_state", "complete" if bool(session.get("goal_completed", False)) else "in_progress")).strip().lower()
    session["goal_progress_state"] = progress_state if progress_state in {"complete", "in_progress"} else "in_progress"
    session["goal_completed"] = session["goal_progress_state"] == "complete"
    # goal_audit_state is agent-side; do not normalize it at session level


def _ensure_session_defaults_unlocked(session: dict[str, Any]) -> None:
    session_id = str(session.get("session_id") or "").strip()
    if not session_id:
        session_id = secrets.token_hex(8)
    session["session_id"] = session_id
    session["auto_compact_threshold_left_percent"] = normalize_auto_compact_threshold_left_percent(
        session.get("auto_compact_threshold_left_percent", DEFAULT_AUTO_COMPACT_THRESHOLD_LEFT_PERCENT)
    )
    session.setdefault("goal_text", "")
    session.setdefault("goal_active", bool(session.get("goal_text")))
    session.setdefault("goal_mode", "active" if bool(session.get("goal_text")) else "no_goal")
    session.setdefault("goal_progress_state", "complete" if bool(session.get("goal_completed", False)) else "in_progress")
    # goal_audit_state is now agent-side; evict any stale field from pre-migration records
    session.pop("goal_audit_state", None)
    session.setdefault("goal_completed", False)
    session.setdefault("goal_reset_completed_on_prompt", True)
    session.setdefault("goal_auto_compact_enabled", True)
    session.setdefault("agent_welcome_enabled", False)
    session.setdefault("preferred_provider", "codex")
    session.setdefault("goal_updated_at", session.get("updated_at", utc_ts()))
    session.setdefault("last_context_status", None)
    session.setdefault("last_context_status_updated_at", session.get("updated_at", utc_ts()))
    group = str(session.get("session_group") or DEFAULT_SESSION_GROUP).strip().lower()
    if group not in SESSION_GROUP_DEFAULT_PERMISSIONS:
        group = DEFAULT_SESSION_GROUP
    session["session_group"] = group
    permissions = session.get("session_permissions")
    if not isinstance(permissions, dict):
        permissions = {}
    defaults = SESSION_GROUP_DEFAULT_PERMISSIONS.get(group, SESSION_GROUP_DEFAULT_PERMISSIONS[DEFAULT_SESSION_GROUP])
    normalized_permissions: dict[str, bool] = {}
    for operation_name, default_value in defaults.items():
        if operation_name in permissions:
            normalized_permissions[operation_name] = bool(permissions.get(operation_name))
        else:
            normalized_permissions[operation_name] = bool(default_value)
    session["session_permissions"] = normalized_permissions
    session.setdefault("auto_resume_enabled", bool(normalized_permissions.get("auto_resume", False)))
    try:
        auto_resume_interval_seconds = int(session.get("auto_resume_interval_seconds", DEFAULT_AUTO_RESUME_INTERVAL_SECONDS))
    except (TypeError, ValueError):
        auto_resume_interval_seconds = DEFAULT_AUTO_RESUME_INTERVAL_SECONDS
    session["auto_resume_interval_seconds"] = max(300, auto_resume_interval_seconds)
    session.setdefault("auto_resume_next_at", "")
    session.setdefault("auto_resume_reason", "")
    session.setdefault("auto_resume_last_scheduled_at", "")
    session.setdefault("auto_resume_last_started_at", "")
    session.setdefault("auto_resume_last_error", "")
    welcomed_agents = session.get("welcomed_agents")
    if not isinstance(welcomed_agents, list):
        welcomed_agents = []
    session["welcomed_agents"] = [dict(item) for item in welcomed_agents if isinstance(item, dict)]
    session.setdefault("goal_manager_last_reviewed_turn_completed_at", "")
    _ensure_goal_history_unlocked(session)


def _ensure_default_session_unlocked(state: dict[str, Any], username: str) -> str:
    normalized = normalize_username(username)
    sessions = _ensure_user_sessions_cache_unlocked(state["_runtime_root"], state, normalized) if "_runtime_root" in state else _conversation_sessions(state).setdefault(normalized, [])
    if sessions:
        for session in sessions:
            if isinstance(session, dict):
                _ensure_session_defaults_unlocked(session)
        return str(sessions[0]["session_id"])
    runtime_root = state["_runtime_root"]
    if not isinstance(runtime_root, Path):
        return "default"
    session = {
        "session_id": "default",
        "label": "Default",
        "auto_compact_threshold_left_percent": DEFAULT_AUTO_COMPACT_THRESHOLD_LEFT_PERCENT,
        "created_at": utc_ts(),
        "updated_at": utc_ts(),
    }
    ensure_session_storage_unlocked(runtime_root, username=normalized, session=session)
    if "conversation_sessions" not in state:
        state["conversation_sessions"] = {}
    _conversation_sessions(state).setdefault(normalized, []).append(session)
    sessions.append(session)
    return str(session["session_id"])


def _ensure_session_exists_unlocked(state: dict[str, Any], username: str, session_id: str) -> bool:
    normalized = normalize_username(username)
    sessions = _ensure_user_sessions_cache_unlocked(state["_runtime_root"], state, normalized) if "_runtime_root" in state else _conversation_sessions(state).setdefault(normalized, [])
    if any(isinstance(session, dict) and str(session.get("session_id") or "") == session_id for session in sessions):
        return True
    # Fallback to on-disk session metadata if the in-memory cache is stale.
    for session in _load_session_records_for_user_unlocked(state["_runtime_root"], normalized):
        if str(session.get("session_id") or "") == session_id:
            sessions.append(session)
            return True
    if session_id == "default":
        _ensure_default_session_unlocked(state, normalized)
        return True
    return False


def _load_state_unlocked(runtime_root: Path) -> dict[str, Any]:
    state = {
        "users": {},
        "auth_sessions": {},
        "conversation_sessions": {},
        "_runtime_root": str(runtime_root),
    }
    path = state_path(runtime_root)
    if not path.exists():
        return state
    state = json.loads(path.read_text(encoding="utf-8"))
    state.setdefault("users", {})
    state.setdefault("auth_sessions", {})
    state.setdefault("conversation_sessions", {})
    state.pop("histories", None)
    state.pop("pending_inputs", None)
    state.pop("service_pending_inputs", None)
    state.pop("codex_sessions", None)
    state.pop("claude_sessions", None)
    state.pop("agent_states", None)
    state["_runtime_root"] = runtime_root
    return state


def ensure_state(runtime_root: Path) -> dict[str, Any]:
    with state_lock(runtime_root):
        path = state_path(runtime_root)
        if path.exists():
            state = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                state = {}
        else:
            state = {}
    state.setdefault("users", {})
    state.setdefault("auth_sessions", {})
    state.setdefault("conversation_sessions", {})
    state.pop("histories", None)
    state.pop("pending_inputs", None)
    state.pop("service_pending_inputs", None)
    state.pop("codex_sessions", None)
    state.pop("claude_sessions", None)
    state.pop("agent_states", None)
    state.pop("sessions", None)
    state.pop("talks", None)
    write_state(runtime_root, state)
    return state
