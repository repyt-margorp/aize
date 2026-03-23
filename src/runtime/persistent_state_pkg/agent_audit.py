from __future__ import annotations

from pathlib import Path

from wire.protocol import utc_ts

from ._core import (
    normalize_username,
    read_json_file,
    session_agent_state_path,
    session_services_dir,
    state_lock,
    write_json_file,
)


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
    with state_lock(runtime_root):
        file_record = read_json_file(
            session_agent_state_path(
                runtime_root,
                username=username,
                session_id=session_id,
                service_id=service_id,
            )
        )
        if isinstance(file_record, dict):
            audit_state = str(file_record.get("audit_state", "all_clear")).strip().lower()
            return audit_state if audit_state in {"all_clear", "needs_compact", "panic"} else "all_clear"
        return "all_clear"


def save_agent_audit_state(
    runtime_root: Path,
    *,
    service_id: str,
    username: str,
    session_id: str,
    audit_state: str,
) -> None:
    """Persist the audit state for an agent-talk binding."""
    normalized = str(audit_state).strip().lower()
    if normalized not in {"all_clear", "needs_compact", "panic"}:
        normalized = "all_clear"
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
