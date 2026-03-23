from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from wire.protocol import utc_ts

from ._core import (
    _auth_sessions,
    _ensure_default_session_unlocked,
    _load_state_unlocked,
    digest_token,
    ensure_session_storage_unlocked,
    hash_password,
    normalize_username,
    read_json_file,
    session_metadata_path,
    state_lock,
    write_state,
)


def has_users(runtime_root: Path) -> bool:
    with state_lock(runtime_root):
        state = _load_state_unlocked(runtime_root)
        return bool(state["users"])


def create_user(runtime_root: Path, *, username: str, password: str) -> tuple[bool, str]:
    normalized = normalize_username(username)
    if not normalized:
        return False, "username_required"
    with state_lock(runtime_root):
        state = _load_state_unlocked(runtime_root)
        if normalized in state["users"]:
            return False, "username_exists"
        salt = secrets.token_bytes(16)
        state["users"][normalized] = {
            "username": normalized,
            "role": "user",
            "password_salt": salt.hex(),
            "password_hash": hash_password(password, salt),
            "created_at": utc_ts(),
        }
        session_id = _ensure_default_session_unlocked(state, normalized)
        ensure_session_storage_unlocked(
            runtime_root,
            username=normalized,
            session=_load_session_record_by_id(
                runtime_root,
                username=normalized,
                session_id=session_id,
            )
            or {"session_id": session_id},
        )
        write_state(runtime_root, state)
    return True, normalized


def bootstrap_root_user(runtime_root: Path, *, password: str) -> tuple[bool, str]:
    with state_lock(runtime_root):
        state = _load_state_unlocked(runtime_root)
        if state["users"]:
            return False, "bootstrap_already_completed"
        salt = secrets.token_bytes(16)
        state["users"]["root"] = {
            "username": "root",
            "role": "superuser",
            "password_salt": salt.hex(),
            "password_hash": hash_password(password, salt),
            "created_at": utc_ts(),
        }
        session_id = _ensure_default_session_unlocked(state, "root")
        ensure_session_storage_unlocked(
            runtime_root,
            username="root",
            session=_load_session_record_by_id(
                runtime_root,
                username="root",
                session_id=session_id,
            )
            or {"session_id": session_id},
        )
        write_state(runtime_root, state)
    return True, "root"


def verify_user_password(runtime_root: Path, *, username: str, password: str) -> bool:
    normalized = normalize_username(username)
    with state_lock(runtime_root):
        state = _load_state_unlocked(runtime_root)
        record = state["users"].get(normalized)
        if not record:
            return False
        expected = str(record["password_hash"])
        actual = hash_password(password, bytes.fromhex(str(record["password_salt"])))
        return secrets.compare_digest(actual, expected)


def create_session(runtime_root: Path, *, username: str) -> str:
    normalized = normalize_username(username)
    token = secrets.token_urlsafe(32)
    token_hash = digest_token(token)
    with state_lock(runtime_root):
        state = _load_state_unlocked(runtime_root)
        session_id = _ensure_default_session_unlocked(state, normalized)
        ensure_session_storage_unlocked(
            runtime_root,
            username=normalized,
            session=_load_session_record_by_id(
                runtime_root,
                username=normalized,
                session_id=session_id,
            )
            or {"session_id": session_id},
        )
        _auth_sessions(state)[token_hash] = {
            "username": normalized,
            "active_session_id": session_id,
            "created_at": utc_ts(),
            "updated_at": utc_ts(),
        }
        write_state(runtime_root, state)
    return token


def resolve_session(runtime_root: Path, token: str | None) -> str | None:
    if not token:
        return None
    token_hash = digest_token(token)
    with state_lock(runtime_root):
        state = _load_state_unlocked(runtime_root)
        session = _auth_sessions(state).get(token_hash)
        if not session:
            return None
        return str(session["username"])


def resolve_session_context(runtime_root: Path, token: str | None) -> dict[str, str] | None:
    if not token:
        return None
    token_hash = digest_token(token)
    with state_lock(runtime_root):
        state = _load_state_unlocked(runtime_root)
        session = _auth_sessions(state).get(token_hash)
        if not session:
            return None
        username = str(session["username"])
        original_active_session_id = str(session.get("active_session_id") or "")
        if original_active_session_id:
            session_id = original_active_session_id
            active_session_record = _load_session_record_by_id(runtime_root, username=username, session_id=session_id)
            if not isinstance(active_session_record, dict):
                session_id = _ensure_default_session_unlocked(state, username)
        else:
            session_id = _ensure_default_session_unlocked(state, username)

        cached = _load_session_record_by_id(runtime_root, username=username, session_id=session_id)
        if cached is None:
            cached = _load_session_record_by_id(runtime_root, username=username, session_id=session_id)
        if isinstance(cached, dict):
            ensure_session_storage_unlocked(runtime_root, username=username, session=cached)
        session["active_session_id"] = session_id
        if session.get("active_session_id") != original_active_session_id:
            write_state(runtime_root, state)
    from kernel.auth import resolve_user_record

    user = resolve_user_record(runtime_root, username=username) or {}
    roles = user.get("roles")
    role_name = str(roles[0]) if isinstance(roles, list) and roles else str(user.get("role", "user"))
    return {"username": username, "session_id": session_id, "role": role_name}


def delete_session(runtime_root: Path, token: str | None) -> None:
    if not token:
        return
    token_hash = digest_token(token)
    with state_lock(runtime_root):
        state = _load_state_unlocked(runtime_root)
        _auth_sessions(state).pop(token_hash, None)
        write_state(runtime_root, state)

def _load_session_record_by_id(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
) -> dict[str, Any] | None:
    normalized = normalize_username(username)
    if not session_id:
        return None
    session = read_json_file(session_metadata_path(runtime_root, username=normalized, session_id=session_id))
    if isinstance(session, dict):
        return session
    return None
