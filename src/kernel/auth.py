from __future__ import annotations

from typing import Any

from runtime.persistent_state import _load_state_unlocked, normalize_username, state_lock, write_state
from wire.protocol import utc_ts


ROLE_CAPABILITIES: dict[str, set[str]] = {
    "root": {"spawn_service", "manage_users", "control_service", "read_service_status", "superuser"},
    "superuser": {"spawn_service", "manage_users", "control_service", "read_service_status", "superuser"},
    "user": set(),
    "system": set(),
}

GOAL_MANAGER_USERNAME = "goalmanager"


def _goal_manager_user_record() -> dict[str, Any]:
    return {
        "username": GOAL_MANAGER_USERNAME,
        "roles": ["system"],
        "capabilities": [],
        "login_disabled": True,
        "system_account": True,
        "created_at": utc_ts(),
    }


def ensure_system_users(runtime_root) -> None:
    with state_lock(runtime_root):
        state = _load_state_unlocked(runtime_root)
        if not isinstance(state["users"].get(GOAL_MANAGER_USERNAME), dict):
            state["users"][GOAL_MANAGER_USERNAME] = _goal_manager_user_record()
            write_state(runtime_root, state)


def has_users(runtime_root) -> bool:
    ensure_system_users(runtime_root)
    with state_lock(runtime_root):
        state = _load_state_unlocked(runtime_root)
        return any(
            isinstance(record, dict) and not bool(record.get("system_account"))
            for record in state["users"].values()
        )


def _normalize_user_record(username: str, record: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_username(username)
    roles = record.get("roles")
    if not isinstance(roles, list) or not all(isinstance(role, str) for role in roles):
        legacy_role = str(record.get("role", "user"))
        roles = ["root", "superuser"] if legacy_role == "superuser" and normalized == "root" else [legacy_role]
    capabilities = record.get("capabilities")
    if not isinstance(capabilities, list) or not all(isinstance(item, str) for item in capabilities):
        derived: set[str] = set()
        for role in roles:
            derived.update(ROLE_CAPABILITIES.get(role, set()))
        capabilities = sorted(derived)
    return {
        **record,
        "username": normalized,
        "roles": roles,
        "capabilities": capabilities,
    }


def _write_user_record(runtime_root, *, username: str, record: dict[str, Any]) -> None:
    with state_lock(runtime_root):
        state = _load_state_unlocked(runtime_root)
        state["users"][username] = record
        write_state(runtime_root, state)


def resolve_user_record(runtime_root, *, username: str) -> dict[str, Any] | None:
    normalized = normalize_username(username)
    ensure_system_users(runtime_root)
    with state_lock(runtime_root):
        state = _load_state_unlocked(runtime_root)
        record = state["users"].get(normalized)
        if not isinstance(record, dict):
            return None
        normalized_record = _normalize_user_record(normalized, record)
        if normalized_record != record:
            state["users"][normalized] = normalized_record
            write_state(runtime_root, state)
        return normalized_record


def bootstrap_root_user(runtime_root, *, password: str) -> tuple[bool, str]:
    with state_lock(runtime_root):
        state = _load_state_unlocked(runtime_root)
        if any(
            isinstance(record, dict) and not bool(record.get("system_account"))
            for record in state["users"].values()
        ):
            return False, "bootstrap_already_completed"
        import secrets
        import hashlib

        salt = secrets.token_bytes(16)
        state["users"]["root"] = {
            "username": "root",
            "roles": ["root", "superuser"],
            "capabilities": sorted(ROLE_CAPABILITIES["root"] | ROLE_CAPABILITIES["superuser"]),
            "password_salt": salt.hex(),
            "password_hash": hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000).hex(),
            "created_at": utc_ts(),
        }
        state["users"].setdefault(GOAL_MANAGER_USERNAME, _goal_manager_user_record())
        write_state(runtime_root, state)
    return True, "root"


def create_user(
    runtime_root,
    *,
    username: str,
    password: str,
    roles: list[str] | None = None,
    capabilities: list[str] | None = None,
) -> tuple[bool, str]:
    normalized = normalize_username(username)
    if not normalized:
        return False, "username_required"
    with state_lock(runtime_root):
        state = _load_state_unlocked(runtime_root)
        state["users"].setdefault(GOAL_MANAGER_USERNAME, _goal_manager_user_record())
        if normalized in state["users"]:
            return False, "username_exists"
        import secrets
        import hashlib

        salt = secrets.token_bytes(16)
        assigned_roles = roles or ["user"]
        assigned_caps = sorted(set(capabilities or []).union(*(ROLE_CAPABILITIES.get(role, set()) for role in assigned_roles)))
        state["users"][normalized] = {
            "username": normalized,
            "roles": assigned_roles,
            "capabilities": assigned_caps,
            "password_salt": salt.hex(),
            "password_hash": hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000).hex(),
            "created_at": utc_ts(),
        }
        write_state(runtime_root, state)
    return True, normalized


def verify_user_password(runtime_root, *, username: str, password: str) -> bool:
    import hashlib
    import secrets

    record = resolve_user_record(runtime_root, username=username)
    if not record:
        return False
    if bool(record.get("login_disabled")):
        return False
    expected = str(record["password_hash"])
    actual = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(str(record["password_salt"])),
        200_000,
    ).hex()
    return secrets.compare_digest(actual, expected)


def issue_auth_context(runtime_root, *, username: str) -> dict[str, Any] | None:
    record = resolve_user_record(runtime_root, username=username)
    if not record:
        return None
    return {
        "principal": str(record["username"]),
        "roles": list(record.get("roles", [])),
        "capabilities": list(record.get("capabilities", [])),
    }


def auth_context_allows(auth: dict[str, Any] | None, capability: str) -> bool:
    if not isinstance(auth, dict):
        return False
    if capability in set(str(item) for item in auth.get("capabilities", [])):
        return True
    roles = {str(item) for item in auth.get("roles", [])}
    for role in roles:
        if capability in ROLE_CAPABILITIES.get(role, set()):
            return True
    return False
