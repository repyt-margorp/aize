from __future__ import annotations

from pathlib import Path

from wire.protocol import utc_ts

from ._core import (
    normalize_username,
    read_json_file,
    session_agent_state_path,
    session_services_dir,
    state_lock,
    state_read_lock,
    write_json_file,
)

AUDIT_STATE_RANK = {"all_clear": 0, "needs_compact": 1, "panic": 2}


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


def load_session_audit_summary(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
) -> dict[str, object]:
    """Return the strongest audit state across every service joined to a session."""
    normalized = normalize_username(username)
    services_dir = session_services_dir(runtime_root, username=normalized, session_id=session_id)
    strongest = "all_clear"
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
            if AUDIT_STATE_RANK[audit_state] > AUDIT_STATE_RANK[strongest]:
                strongest = audit_state
        return {"audit_state": strongest, "services": services}


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
