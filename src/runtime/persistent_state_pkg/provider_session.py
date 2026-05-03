from __future__ import annotations

from pathlib import Path
from typing import Any

from ._core import (
    normalize_username,
    read_json_file,
    sessions_dir,
    session_metadata_path,
    session_service_pending_path,
    session_service_state_path,
    state_lock,
    write_json_file,
)

_PROVIDER_SESSION_FIELDS = {
    "codex": "codex_session_id",
    "claude": "claude_session_id",
    "gemini": "gemini_session_id",
}
DEFAULT_PROVIDER_SESSION_SLOT = "worker_agent"


def _normalize_provider_session_slot(slot: str | None) -> str:
    normalized = "".join(
        ch if ch.isalnum() or ch in {"_", "-", "."} else "_"
        for ch in str(slot or "").strip().lower()
    ).strip("._-")
    return normalized or DEFAULT_PROVIDER_SESSION_SLOT


def _load_provider_session_id(
    service_state: dict[str, Any],
    *,
    provider: str,
    slot: str | None,
) -> str | None:
    field = _PROVIDER_SESSION_FIELDS[provider]
    normalized_slot = _normalize_provider_session_slot(slot)
    provider_sessions = service_state.get("provider_sessions")
    if isinstance(provider_sessions, dict):
        slot_state = provider_sessions.get(normalized_slot)
        if isinstance(slot_state, dict):
            provider_session_id = slot_state.get(field)
            if isinstance(provider_session_id, str) and provider_session_id:
                return provider_session_id
    return None


def _load_session(
    runtime_root: Path,
    *,
    provider: str,
    service_id: str,
    username: str | None = None,
    session_id: str | None = None,
    slot: str | None = None,
) -> str | None:
    with state_lock(runtime_root):
        if username and session_id:
            service_state = read_json_file(
                session_service_state_path(
                    runtime_root,
                    username=username,
                    session_id=session_id,
                    service_id=service_id,
                )
            )
            if isinstance(service_state, dict):
                return _load_provider_session_id(service_state, provider=provider, slot=slot)
        return None


def _save_session(
    runtime_root: Path,
    *,
    provider: str,
    service_id: str,
    provider_session_id: str | None,
    username: str | None = None,
    session_id: str | None = None,
    slot: str | None = None,
) -> None:
    with state_lock(runtime_root):
        if not (username and session_id):
            return
        field = _PROVIDER_SESSION_FIELDS[provider]
        normalized_slot = _normalize_provider_session_slot(slot)
        service_state_path = session_service_state_path(
            runtime_root,
            username=username,
            session_id=session_id,
            service_id=service_id,
        )
        service_state = read_json_file(service_state_path) or {"service_id": service_id}
        provider_sessions = service_state.get("provider_sessions")
        if not isinstance(provider_sessions, dict):
            provider_sessions = {}
        slot_state = provider_sessions.get(normalized_slot)
        if not isinstance(slot_state, dict):
            slot_state = {}
        slot_state["slot"] = normalized_slot
        slot_state["provider"] = provider
        if provider_session_id:
            slot_state[field] = provider_session_id
        else:
            slot_state.pop(field, None)
        if provider_session_id or any(key in slot_state for key in _PROVIDER_SESSION_FIELDS.values()):
            provider_sessions[normalized_slot] = slot_state
        else:
            provider_sessions.pop(normalized_slot, None)
        if provider_sessions:
            service_state["provider_sessions"] = provider_sessions
        else:
            service_state.pop("provider_sessions", None)
        service_state.pop(field, None)
        write_json_file(service_state_path, service_state)


def load_claude_session(
    runtime_root: Path,
    *,
    service_id: str,
    username: str | None = None,
    session_id: str | None = None,
    slot: str | None = None,
) -> str | None:
    return _load_session(
        runtime_root,
        provider="claude",
        service_id=service_id,
        username=username,
        session_id=session_id,
        slot=slot,
    )


def load_gemini_session(
    runtime_root: Path,
    *,
    service_id: str,
    username: str | None = None,
    session_id: str | None = None,
    slot: str | None = None,
) -> str | None:
    return _load_session(
        runtime_root,
        provider="gemini",
        service_id=service_id,
        username=username,
        session_id=session_id,
        slot=slot,
    )


def save_claude_session(
    runtime_root: Path,
    *,
    service_id: str,
    provider_session_id: str | None,
    username: str | None = None,
    session_id: str | None = None,
    slot: str | None = None,
) -> None:
    _save_session(
        runtime_root,
        provider="claude",
        service_id=service_id,
        provider_session_id=provider_session_id,
        username=username,
        session_id=session_id,
        slot=slot,
    )


def save_gemini_session(
    runtime_root: Path,
    *,
    service_id: str,
    provider_session_id: str | None,
    username: str | None = None,
    session_id: str | None = None,
    slot: str | None = None,
) -> None:
    _save_session(
        runtime_root,
        provider="gemini",
        service_id=service_id,
        provider_session_id=provider_session_id,
        username=username,
        session_id=session_id,
        slot=slot,
    )


def clear_session_service_runtime(
    runtime_root: Path,
    *,
    username: str,
    session_id: str,
    service_id: str,
) -> None:
    normalized = normalize_username(username)
    normalized_service_id = str(service_id or "").strip()
    if not normalized_service_id:
        return
    with state_lock(runtime_root):
        service_state_path = session_service_state_path(
            runtime_root,
            username=normalized,
            session_id=session_id,
            service_id=normalized_service_id,
        )
        audit_state_path = service_state_path.with_suffix(".audit.json")
        pending_path = session_service_pending_path(
            runtime_root,
            username=normalized,
            session_id=session_id,
            service_id=normalized_service_id,
        )
        for path in (service_state_path, audit_state_path, pending_path):
            try:
                path.unlink()
            except FileNotFoundError:
                continue


def load_codex_session(
    runtime_root: Path,
    *,
    service_id: str,
    username: str | None = None,
    session_id: str | None = None,
    slot: str | None = None,
) -> str | None:
    return _load_session(
        runtime_root,
        provider="codex",
        service_id=service_id,
        username=username,
        session_id=session_id,
        slot=slot,
    )


def _iter_provider_session_items(
    service_state: dict[str, Any],
    *,
    provider: str,
) -> list[tuple[str, str]]:
    field = _PROVIDER_SESSION_FIELDS[provider]
    provider_sessions = service_state.get("provider_sessions")
    if not isinstance(provider_sessions, dict):
        return []
    items: list[tuple[str, str]] = []
    for raw_slot, raw_slot_state in sorted(provider_sessions.items()):
        if not isinstance(raw_slot_state, dict):
            continue
        slot = _normalize_provider_session_slot(str(raw_slot_state.get("slot") or raw_slot))
        provider_session_id = raw_slot_state.get(field)
        if isinstance(provider_session_id, str) and provider_session_id:
            items.append((slot, provider_session_id))
    return items


def _list_provider_sessions(
    runtime_root: Path,
    *,
    service_id: str,
    provider: str,
) -> list[dict[str, str | None]]:
    with state_lock(runtime_root):
        sessions: list[dict[str, str | None]] = []
        seen: set[tuple[str | None, str | None, str | None, str | None]] = set()
        sessions_root = sessions_dir(runtime_root)
        if sessions_root.exists():
            for user_dir in sorted(path for path in sessions_root.iterdir() if path.is_dir()):
                username = normalize_username(user_dir.name)
                for talk_dir in sorted(path for path in user_dir.iterdir() if path.is_dir()):
                    session_metadata = read_json_file(
                        session_metadata_path(runtime_root, username=username, session_id=talk_dir.name)
                    )
                    if not isinstance(session_metadata, dict):
                        continue
                    if str(session_metadata.get("_runtime_root") or "") != str(runtime_root):
                        continue
                    service_state = read_json_file(
                        session_service_state_path(
                            runtime_root,
                            username=username,
                            session_id=talk_dir.name,
                            service_id=service_id,
                        )
                    )
                    if not isinstance(service_state, dict):
                        continue
                    for slot, provider_session_id in _iter_provider_session_items(service_state, provider=provider):
                        item = {
                            "username": username,
                            "conversation_session_id": talk_dir.name,
                            "slot": slot,
                            "session_id": provider_session_id,
                        }
                        key = (item["username"], item["conversation_session_id"], item["slot"], item["session_id"])
                        if key not in seen:
                            seen.add(key)
                            sessions.append(item)
        return sessions


def list_codex_sessions(runtime_root: Path, *, service_id: str) -> list[dict[str, str | None]]:
    return _list_provider_sessions(runtime_root, service_id=service_id, provider="codex")


def list_claude_sessions(runtime_root: Path, *, service_id: str) -> list[dict[str, str | None]]:
    return _list_provider_sessions(runtime_root, service_id=service_id, provider="claude")


def list_gemini_sessions(runtime_root: Path, *, service_id: str) -> list[dict[str, str | None]]:
    return _list_provider_sessions(runtime_root, service_id=service_id, provider="gemini")


def save_codex_session(
    runtime_root: Path,
    *,
    service_id: str,
    provider_session_id: str | None,
    username: str | None = None,
    session_id: str | None = None,
    slot: str | None = None,
) -> None:
    _save_session(
        runtime_root,
        provider="codex",
        service_id=service_id,
        provider_session_id=provider_session_id,
        username=username,
        session_id=session_id,
        slot=slot,
    )
